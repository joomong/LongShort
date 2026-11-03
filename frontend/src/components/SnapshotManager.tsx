'use client';

import { useRunStore } from '@/runStore';

export default function SnapshotManager() {
  const pinnedSnapshots = useRunStore(s => s.pinnedSnapshots);
  const removeSnapshot = useRunStore(s => s.removeSnapshot);
  const updateSnapshotName = useRunStore(s => s.updateSnapshotName);

  if (pinnedSnapshots.length === 0) return null;

  return (
    <div className="px-2 py-1 border-t border-border bg-background shrink-0">
      <div className="text-[11px] font-medium text-foreground-secondary mb-1">Pinned Snapshots</div>
      <div className="flex flex-col gap-1">
        {pinnedSnapshots.map((snap) => (
          <div key={snap.id} className="flex items-center gap-2">
            <span
              className="inline-block border-l-[3px] px-1.5 py-0.5 text-[10px] font-mono tabular-nums text-foreground-secondary shrink-0"
              style={{ borderColor: snap.color }}
            >
              {snap.signalSeries.timestamps.length}pts
            </span>
            <input
              className="flex-1 min-w-0 text-[11px] bg-transparent border-b border-border text-foreground px-1 py-0.5 focus:outline-none focus:border-accent"
              value={snap.name}
              onChange={(e) => updateSnapshotName(snap.id, e.target.value)}
              aria-label="Snapshot name"
            />
            <button
              className="text-xs text-foreground-muted hover:text-metric-negative px-1 focus-visible:ring-2 focus-visible:ring-accent"
              onClick={() => removeSnapshot(snap.id)}
              title="Remove snapshot"
              aria-label="Remove snapshot"
            >
              x
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
