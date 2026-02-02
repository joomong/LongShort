'use client';

import { useAlphaStore } from '@/store/alphaStore';

export default function SnapshotManager() {
  const { pinnedSnapshots, removeSnapshot, updateSnapshotName } = useAlphaStore();

  if (pinnedSnapshots.length === 0) return null;

  return (
    <div className="px-4 py-2 border-t border-border bg-background-tertiary shrink-0">
      <div className="text-xs text-foreground-muted mb-1">Pinned Snapshots</div>
      <div className="flex flex-col gap-1">
        {pinnedSnapshots.map((snap) => (
          <div key={snap.id} className="flex items-center gap-2">
            <span
              className="inline-block rounded px-1.5 py-0.5 text-xs font-medium text-white shrink-0"
              style={{ backgroundColor: snap.color }}
            >
              {Object.keys(snap.retSeries).length}pts
            </span>
            <input
              className="flex-1 min-w-0 text-xs bg-transparent border-b border-border text-foreground px-1 py-0.5 focus:outline-none focus:border-accent"
              value={snap.name}
              onChange={(e) => updateSnapshotName(snap.id, e.target.value)}
            />
            <button
              className="text-xs text-foreground-muted hover:text-red-400 px-1"
              onClick={() => removeSnapshot(snap.id)}
              title="Remove snapshot"
            >
              x
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
