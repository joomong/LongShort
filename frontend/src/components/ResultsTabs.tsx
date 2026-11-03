'use client';

import { useRunStore } from '@/runStore';
import { useEffect } from 'react';

export default function ResultsTabs() {
  const rounds = useRunStore(s => s.gaRounds);
  const active = useRunStore(s => s.activeResultTab);
  const tabs: { id: 'stats' | number; label: string }[] = [
    { id: 'stats', label: 'Stats' },
    ...rounds.map(round => ({ id: round.round, label: `Round ${round.round}` })),
  ];
  useEffect(() => {
    document.getElementById(`results-tab-${active}`)?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [active]);
  return (
    <div role="tablist" aria-label="Alpha result tabs" className="flex shrink-0 min-h-[27px] items-end gap-[3px] overflow-x-auto bg-[#edb54b] px-[3px] pt-[3px] border-b border-[#dca436]">
      {tabs.map(tab => (
        <button key={tab.id} role="tab" id={`results-tab-${tab.id}`} aria-controls="alpha-results-panel"
          aria-selected={active === tab.id} tabIndex={active === tab.id ? 0 : -1}
          onClick={() => useRunStore.getState().selectResultTab(tab.id)}
          onKeyDown={event => {
            const index = tabs.findIndex(item => item.id === tab.id);
            const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index - 1 + tabs.length) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
            if (next < 0) return;
            event.preventDefault();
            useRunStore.getState().selectResultTab(tabs[next].id);
            document.getElementById(`results-tab-${tabs[next].id}`)?.focus();
          }}
          className={`shrink-0 min-w-[62px] h-[24px] px-3 rounded-t-[2px] border border-b-0 border-[#dba13b] bg-[#fffdfa] text-[11px] font-sans font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#5f91aa] ${active === tab.id ? 'text-[#c9912c]' : 'text-[#6193ac] hover:text-[#c9912c]'}`}>
          {tab.label}
        </button>
      ))}
    </div>
  );
}
