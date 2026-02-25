'use client';

import { useAlphaStore } from '@/store/alphaStore';

export default function SnapshotManager() {
  const pinnedSnapshots = useAlphaStore(s => s.pinnedSnapshots);
  const removeSnapshot = useAlphaStore(s => s.removeSnapshot);
  const updateSnapshotName = useAlphaStore(s => s.updateSnapshotName);

  if (pinnedSnapshots.length === 0) return null;

  return (
    <div className="px-4 py-2 border-t border-border bg-background-secondary shrink-0 panel-inset">
      <div className="text-xs text-foreground-muted mb-1">Pinned Snapshots</div>
      <div className="flex flex-col gap-1">
        {pinnedSnapshots.map((snap) => (
          <div key={snap.id} className="flex items-center gap-2">
            <span
              className="inline-block px-1.5 py-0.5 text-xs font-medium text-white shrink-0"
              style={{ backgroundColor: snap.color }}
            >
              {Object.keys(snap.retSeries).length}pts
            </span>
            <input
              className="flex-1 min-w-0 text-xs bg-transparent border-b border-border text-foreground px-1 py-0.5 focus:outline-none focus:border-accent"
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
