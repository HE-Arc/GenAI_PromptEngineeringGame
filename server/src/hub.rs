use serde_json::Value;
use sqlx::SqlitePool;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::sync::{broadcast, Mutex, RwLock};

use crate::db::rooms;
use crate::ws::protocol::WsOut;

pub type SnapshotMsg = (u64, Value);
pub type Hubs = RwLock<HashMap<String, Arc<RoomHub>>>;

pub struct RoomHub {
    tx: broadcast::Sender<SnapshotMsg>,
    snapshot: RwLock<SnapshotMsg>,
    // Fan-out for answer.chunk/scoring/done: chunk streaming must not touch
    // the DB, so live frames travel here while snapshots stay cached.
    events: broadcast::Sender<WsOut>,
    // One live LLM stream per (round, player); replaces the Django
    // process-local _inflight set so every Tokio task sees the same claims.
    inflight: Mutex<HashSet<(i64, i64)>>,
    // Connected player sockets (admins excluded). Atomic because connect
    // and disconnect run on different Tokio tasks and share no await point.
    presence: AtomicUsize,
}

impl RoomHub {
    pub fn new() -> Self {
        Self::new_with_capacity(256)
    }

    pub fn new_with_capacity(capacity: usize) -> Self {
        let (tx, _rx) = broadcast::channel(capacity);
        let (events, _erx) = broadcast::channel(capacity);
        Self {
            tx,
            snapshot: RwLock::new((0, Value::Null)),
            events,
            inflight: Mutex::new(HashSet::new()),
            presence: AtomicUsize::new(0),
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

    // Player sockets only; admin/cookie connections never count.
    pub fn presence(&self) -> usize {
        self.presence.load(Ordering::Relaxed)
    }

    // Connect/disconnect mutate only the in-memory count, yet every
    // subscriber (and the admin list) must see the new value live: rebuild
    // the snapshot once per change and bump the seq like any other write.
    // Returns the fresh (seq, state) so the hello sender can reply with it.
    pub async fn refresh_presence(
        &self,
        pool: &SqlitePool,
        code: &str,
        delta: i64,
    ) -> Option<SnapshotMsg> {
        let prev = if delta > 0 {
            self.presence.fetch_add(delta as usize, Ordering::Relaxed)
        } else {
            // usize counter, isize delta: saturate instead of panicking on underflow.
            let cur = self.presence.load(Ordering::Relaxed);
            let sub: usize = delta.unsigned_abs().try_into().unwrap_or(usize::MAX);
            let next = cur.saturating_sub(sub);
            self.presence.store(next, Ordering::Relaxed);
            cur
        };
        let new = self.presence.load(Ordering::Relaxed);
        if new == prev {
            return None;
        }
        let snap = rooms::room_snapshot(pool, code, Some(new)).await.ok()?;
        let seq = self.next_seq().await;
        self.publish_snapshot(seq, snap).await;
        Some(self.cached().await)
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
    async fn presence_increment_decrement_publishes_live_count() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "PR23").await.unwrap();
        // Presence is clamped to the roster, so a player row must exist.
        crate::db::rooms::add_player(&pool, "PR23", "ana").await.unwrap();
        let hub = RoomHub::new();
        assert_eq!(hub.presence(), 0);

        // Connect: one player socket => online_count 1 in the new snapshot.
        let (seq1, snap1) = hub
            .refresh_presence(&pool, "PR23", 1)
            .await
            .expect("snapshot");
        assert_eq!(hub.presence(), 1);
        assert_eq!(snap1["online_count"], 1);
        assert!(seq1 >= 1);

        // Disconnect: count falls back to 0 and a fresh snapshot goes out.
        let (seq2, snap2) = hub
            .refresh_presence(&pool, "PR23", -1)
            .await
            .expect("snapshot");
        assert_eq!(hub.presence(), 0);
        assert_eq!(snap2["online_count"], 0);
        assert!(seq2 > seq1, "disconnect must advance the snapshot seq");

        // Saturating guard: a stale decrement can never panic or go negative.
        let _ = hub.refresh_presence(&pool, "PR23", -1).await;
        assert_eq!(hub.presence(), 0);
    }
}
