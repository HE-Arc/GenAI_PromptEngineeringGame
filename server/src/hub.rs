use serde_json::Value;
use sqlx::SqlitePool;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{broadcast, Mutex, RwLock};

use crate::db::rooms;
use crate::ws::protocol::WsOut;

pub type SnapshotMsg = (u64, Value);
pub type Hubs = RwLock<HashMap<String, Arc<RoomHub>>>;

// A closed socket is not a gone player: a phone that locks, a tab in the
// background or a Wi-Fi flap all drop the socket while the player is still
// in the game. Only a player unreachable for this long is shown offline.
pub const OFFLINE_GRACE: Duration = Duration::from_secs(60);

// How long a room-wide failure notice stays in the snapshot.
pub const ROOM_ERROR_TTL: Duration = Duration::from_secs(15);

// One entry per player who is online or inside the grace window. `epoch`
// changes on every connect, so a grace timer armed before a reconnect can
// tell it is stale and must not evict the player.
struct Seat {
    sockets: usize,
    epoch: u64,
}

#[derive(Default)]
struct Presence {
    seats: HashMap<i64, Seat>,
    next_epoch: u64,
}

pub struct RoomHub {
    tx: broadcast::Sender<SnapshotMsg>,
    snapshot: RwLock<SnapshotMsg>,
    // Fan-out for answer.chunk/scoring/done: chunk streaming must not touch
    // the DB, so live frames travel here while snapshots stay cached.
    events: broadcast::Sender<WsOut>,
    // One live LLM stream per (round, player); replaces the Django
    // process-local _inflight set so every Tokio task sees the same claims.
    inflight: Mutex<HashSet<(i64, i64)>>,
    // Online players by id (admins excluded). A std Mutex: every critical
    // section is a few map ops with no await inside.
    presence: std::sync::Mutex<Presence>,
    // Transient failure notice (`state.error`): a French sentence and the
    // instant it expires.
    error: std::sync::Mutex<Option<(Instant, String)>>,
    // Last instant this room was opened or had a player online; the idle
    // sweep hides and deletes rooms by it.
    last_seen: std::sync::Mutex<Instant>,
    // Republish coalescing: `requested` counts calls, `built` is the request
    // count covered by the last published build. Builds run one at a time
    // under `build_lock`, so a snapshot read before a write can never be
    // published after one read after it.
    requested: std::sync::atomic::AtomicU64,
    built: std::sync::atomic::AtomicU64,
    build_lock: Mutex<()>,
}

// 1024 broadcast slots: a 30-player answer burst fans ~30 chunk frames at
// once, and 256 lagged a full classroom into resync storms. Memory stays
// trivial (frames are small JSON), lag still resyncs from the cache.
pub const BROADCAST_CAP: usize = 1024;

impl RoomHub {
    pub fn new() -> Self {
        Self::new_with_capacity(BROADCAST_CAP)
    }

    pub fn new_with_capacity(capacity: usize) -> Self {
        let (tx, _rx) = broadcast::channel(capacity);
        let (events, _erx) = broadcast::channel(capacity);
        Self {
            tx,
            snapshot: RwLock::new((0, Value::Null)),
            events,
            inflight: Mutex::new(HashSet::new()),
            presence: std::sync::Mutex::new(Presence::default()),
            error: std::sync::Mutex::new(None),
            last_seen: std::sync::Mutex::new(Instant::now()),
            requested: std::sync::atomic::AtomicU64::new(0),
            built: std::sync::atomic::AtomicU64::new(0),
            build_lock: Mutex::new(()),
        }
    }

    // Single snapshot build per mutation; subscribers clone the cache.
    // The socket forwarder listens on `events` only, so the snapshot must be
    // fanned there too — otherwise phase changes never reach a live client
    // and players only see them after a reload.
    pub async fn publish_snapshot(&self, seq: u64, state: Value) {
        *self.snapshot.write().await = (seq, state.clone());
        let _ = self.tx.send((seq, state.clone()));
        let _ = self.events.send(WsOut::Snapshot { seq, state });
    }

    // Next seq derived from the cache so concurrent writers cannot go backwards.
    pub async fn next_seq(&self) -> u64 {
        self.cached().await.0 + 1
    }

    pub async fn cached(&self) -> SnapshotMsg {
        self.snapshot.read().await.clone()
    }

    pub async fn subscribe(&self) -> broadcast::Receiver<SnapshotMsg> {
        self.tx.subscribe()
    }

    pub fn publish_event(&self, msg: WsOut) {
        let _ = self.events.send(msg);
    }

    pub async fn subscribe_events(&self) -> broadcast::Receiver<WsOut> {
        self.events.subscribe()
    }

    // False means a stream for this (round, player) is already running: the
    // caller must answer error inflight, not spawn a second task.
    pub async fn try_claim(&self, round_id: i64, player_id: i64) -> bool {
        self.inflight.lock().await.insert((round_id, player_id))
    }

    pub async fn release(&self, round_id: i64, player_id: i64) {
        self.inflight.lock().await.remove(&(round_id, player_id));
    }

    // A round's clock must not close it while an answer is still streaming.
    pub async fn round_inflight(&self, round_id: i64) -> bool {
        self.inflight.lock().await.iter().any(|(r, _)| *r == round_id)
    }

    // Lagged receivers must resync from the cache, never panic on RecvError::Lagged.
    // Drains the stale queue; caller sends cached() as the fresh snapshot.
    pub fn resync_needed(&self, rx: &mut broadcast::Receiver<SnapshotMsg>) -> bool {
        loop {
            match rx.try_recv() {
                Err(broadcast::error::TryRecvError::Lagged(_)) => return true,
                Err(_) => return false,
                Ok(_) => continue,
            }
        }
    }

    // Players only; admin/cookie connections never count.
    pub fn presence(&self) -> usize {
        self.presence.lock().unwrap().seats.len()
    }

    pub fn online(&self) -> HashSet<i64> {
        self.presence.lock().unwrap().seats.keys().copied().collect()
    }

    // Snapshot rebuild with the live online set: the one way every writer
    // builds a snapshot, so presence never regresses across a DB write.
    //
    // Callers republish AFTER their write commits. Under the lock, a build
    // that started after this call's request already read that write, so a
    // burst of N writes costs a handful of builds instead of N racing ones
    // whose publish order no longer matches their read order.
    pub async fn republish(&self, pool: &SqlitePool, code: &str) -> Option<SnapshotMsg> {
        use std::sync::atomic::Ordering::SeqCst;
        let mine = self.requested.fetch_add(1, SeqCst) + 1;
        let _guard = self.build_lock.lock().await;
        if self.built.load(SeqCst) >= mine {
            return Some(self.cached().await);
        }
        let covers = self.requested.load(SeqCst);
        let online = self.online();
        let mut snap = rooms::room_snapshot(pool, code, Some(&online)).await.ok()?;
        snap["error"] = self.current_error().into();
        self.built.store(covers, SeqCst);
        let seq = self.next_seq().await;
        self.publish_snapshot(seq, snap).await;
        Some(self.cached().await)
    }

    pub fn note_error(&self, message: &str) {
        *self.error.lock().unwrap() = Some((Instant::now() + ROOM_ERROR_TTL, message.to_owned()));
    }

    // The live notice, clearing it once expired.
    pub fn current_error(&self) -> Option<String> {
        let mut e = self.error.lock().unwrap();
        match e.as_ref() {
            Some((expiry, msg)) if Instant::now() < *expiry => Some(msg.clone()),
            _ => {
                *e = None;
                None
            }
        }
    }

    // A notice that has expired but is still on screen: the ticker
    // republishes once so it disappears without another write.
    pub fn error_needs_clearing(&self) -> bool {
        matches!(self.error.lock().unwrap().as_ref(), Some((expiry, _)) if Instant::now() >= *expiry)
    }

    pub fn mark_seen(&self) {
        *self.last_seen.lock().unwrap() = Instant::now();
    }

    // Seconds since the room was last opened or had someone online; an
    // online room is "now".
    pub fn idle_for(&self) -> Duration {
        if self.presence() > 0 {
            self.mark_seen();
        }
        self.last_seen.lock().unwrap().elapsed()
    }

    // Registers one more socket for this player. Republishes only when the
    // player was not already online: a reconnect inside the grace window
    // changes nothing anyone can see.
    pub async fn player_connected(&self, pool: &SqlitePool, code: &str, player_id: i64) {
        self.mark_seen();
        let newly_online = {
            let mut p = self.presence.lock().unwrap();
            p.next_epoch += 1;
            let epoch = p.next_epoch;
            let newly_online = !p.seats.contains_key(&player_id);
            let seat = p.seats.entry(player_id).or_insert(Seat { sockets: 0, epoch });
            seat.epoch = epoch;
            seat.sockets += 1;
            newly_online
        };
        if newly_online {
            let _ = self.republish(pool, code).await;
        }
    }

    // Drops one socket. The last socket closing does not take the player
    // offline: a timer does, after OFFLINE_GRACE, unless they came back.
    pub fn player_disconnected(self: &Arc<Self>, pool: SqlitePool, code: String, player_id: i64) {
        self.player_disconnected_after(pool, code, player_id, OFFLINE_GRACE);
    }

    fn player_disconnected_after(
        self: &Arc<Self>,
        pool: SqlitePool,
        code: String,
        player_id: i64,
        grace: Duration,
    ) {
        let armed = {
            let mut p = self.presence.lock().unwrap();
            match p.seats.get_mut(&player_id) {
                Some(seat) => {
                    seat.sockets = seat.sockets.saturating_sub(1);
                    (seat.sockets == 0).then_some(seat.epoch)
                }
                None => None,
            }
        };
        let Some(epoch) = armed else { return };
        let hub = Arc::clone(self);
        tokio::spawn(async move {
            tokio::time::sleep(grace).await;
            let evicted = {
                let mut p = hub.presence.lock().unwrap();
                let stale = matches!(
                    p.seats.get(&player_id),
                    Some(seat) if seat.sockets == 0 && seat.epoch == epoch
                );
                if stale {
                    p.seats.remove(&player_id);
                }
                stale
            };
            if evicted {
                hub.mark_seen();
                // One fewer present player lowers the majority: the votes
                // already cast may now be enough to move the room.
                crate::game::maybe_advance_by_votes(&pool, &code, &hub.online()).await;
                let _ = hub.republish(&pool, &code).await;
            }
        });
    }
}

impl Default for RoomHub {
    fn default() -> Self {
        Self::new()
    }
}

pub async fn get_hub(hubs: &Hubs, code: &str) -> Arc<RoomHub> {
    if let Some(hub) = hubs.read().await.get(code) {
        return Arc::clone(hub);
    }
    let mut write = hubs.write().await;
    Arc::clone(write.entry(code.to_string()).or_insert_with(|| Arc::new(RoomHub::new())))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn subscriber_gets_cached_snapshot_without_db_hit() {
        let hub = RoomHub::new();
        hub.publish_snapshot(1, json!({"players": [{"name": "ana", "online": true}]})).await;
        let (seq, snap) = hub.cached().await;
        assert_eq!((seq, snap["players"][0]["name"].clone()), (1, json!("ana")));
        // Second subscriber clones the cache; no DB call exists on this path by construction.
        let (seq2, snap2) = hub.cached().await;
        assert_eq!((seq2, snap2["players"][0]["name"].clone()), (1, json!("ana")));
    }

    #[tokio::test]
    async fn lagged_receiver_resyncs() {
        let hub = RoomHub::new_with_capacity(1);
        let mut rx = hub.subscribe().await;
        hub.publish_snapshot(1, json!({})).await;
        hub.publish_snapshot(2, json!({})).await;
        hub.publish_snapshot(3, json!({})).await;
        assert!(hub.resync_needed(&mut rx));
        let (seq, _) = hub.cached().await;
        assert_eq!(seq, 3);
    }

    #[tokio::test]
    async fn disconnect_waits_for_grace_and_reconnect_cancels_it() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "PR23").await.unwrap();
        let ana = crate::db::rooms::add_player(&pool, "PR23", "ana").await.unwrap();
        let hub = Arc::new(RoomHub::new());
        let grace = Duration::from_millis(50);

        hub.player_connected(&pool, "PR23", ana.id).await;
        let (seq1, snap1) = hub.cached().await;
        assert_eq!(snap1["online_count"], 1);
        assert_eq!(snap1["players"][0]["online"], true);

        // Socket drops, player comes back inside the grace window: no flap.
        hub.player_disconnected_after(pool.clone(), "PR23".into(), ana.id, grace);
        assert_eq!(hub.presence(), 1, "closed socket must not mean offline");
        hub.player_connected(&pool, "PR23", ana.id).await;
        tokio::time::sleep(grace * 3).await;
        assert_eq!(hub.presence(), 1, "stale grace timer must not evict");
        assert_eq!(hub.cached().await.0, seq1, "reconnect publishes nothing");

        // Two sockets for one player count once; only the last close arms.
        hub.player_connected(&pool, "PR23", ana.id).await;
        hub.player_disconnected_after(pool.clone(), "PR23".into(), ana.id, grace);
        tokio::time::sleep(grace * 3).await;
        assert_eq!(hub.presence(), 1);

        // Last socket gone and nobody returns: offline after the grace.
        hub.player_disconnected_after(pool.clone(), "PR23".into(), ana.id, grace);
        tokio::time::sleep(grace * 3).await;
        assert_eq!(hub.presence(), 0);
        let (seq2, snap2) = hub.cached().await;
        assert!(seq2 > seq1);
        assert_eq!(snap2["online_count"], 0);
        assert_eq!(snap2["players"][0]["online"], false);
    }
}
