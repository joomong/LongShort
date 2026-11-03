'use client';

import { useMemo } from 'react';
import { useRunStore } from '@/runStore';

// React escapes the tokens: XML stays selectable text, never executable HTML.
function highlightXml(xml: string) {
  return xml.split(/(<!--[\s\S]*?-->|<\/?[\w:.-]+|\b[\w:.-]+(?=\s*=)|"[^"]*"|'[^']*'|\/?>)/g).map((token, index) => {
    const color = token.startsWith('<!--')
      ? 'text-foreground-muted'
      : /^['"]/.test(token)
        ? 'text-green-700 dark:text-green-400'
        : token.startsWith('<') || token === '>' || token === '/>'
          ? 'text-red-700 dark:text-red-400'
          : /^[\w:.-]+$/.test(token)
            ? 'text-blue-700 dark:text-blue-400'
            : undefined;
    return <span key={index} className={color}>{token}</span>;
  });
}

export default function AlphaXmlPanel() {
  const alpha = useRunStore(s => {
    const index = s.selectedAlphaIndex;
    return index === -1 ? s.result?.combined : index !== null ? s.result?.alphas?.[index] : undefined;
  });
  const xml = alpha?.xml ?? '';
  const highlighted = useMemo(() => highlightXml(xml), [xml]);

  return (
    <section aria-label="Selected alpha XML" className="flex flex-col h-full min-h-0 min-w-0 border-l border-border bg-chart-background">
      <pre className="flex-1 min-h-0 overflow-auto px-2 py-1 text-[11px] font-mono text-data-color whitespace-pre-wrap break-words leading-[17px]" tabIndex={0}>
        {xml ? highlighted : 'No XML available for this alpha.'}
      </pre>
    </section>
  );
}
