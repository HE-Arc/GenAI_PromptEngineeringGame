use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::{broadcast, Mutex, RwLock};

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
        }
    }

    // Single snapshot build per mutation; subscribers clone the cache.
    pub async fn publish_snapshot(&self, seq: u64, state: Value) {
        *self.snapshot.write().await = (seq, state.clone());
        let _ = self.tx.send((seq, state));
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
        hub.publish_snapshot(1, json!({"players": ["ana"]})).await;
        let (seq, snap) = hub.cached().await;
        assert_eq!((seq, snap["players"][0].clone()), (1, json!("ana")));
        // Second subscriber clones the cache; no DB call exists on this path by construction.
        let (seq2, snap2) = hub.cached().await;
        assert_eq!((seq2, snap2["players"][0].clone()), (1, json!("ana")));
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
}
