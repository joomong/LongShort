'use client';

import { useMemo } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';
import dynamic from 'next/dynamic';

// Monaco Editor is client-only, so we need to dynamically import it
const Editor = dynamic(() => import('@monaco-editor/react'), { ssr: false });

export default function XmlEditor() {
  const { xmlCode, setXmlCode, viewMode, selectedAlphaIndex, result } = useAlphaStore();
  const { theme } = useThemeStore();

  const selectedAlphaXml = useMemo(() => {
    if (viewMode !== 'detail') return null;
    if (selectedAlphaIndex === null) return null;
    const alpha = result?.alphas?.[selectedAlphaIndex];
    return alpha?.xml ?? null;
  }, [viewMode, selectedAlphaIndex, result]);

  const isDetailXml = Boolean(selectedAlphaXml);
  const value = selectedAlphaXml ?? xmlCode;

  return (
    <div className="flex-1 w-full h-full">
      <Editor
        height="100%"
        defaultLanguage="xml"
        value={value}
        onChange={(v) => {
          // In detail mode, show per-alpha XML read-only (do not overwrite main editor contents).
          if (isDetailXml) return;
          setXmlCode(v || '');
        }}
        theme={theme === 'dark' ? 'vs-dark' : 'light'}
        options={{
          minimap: { enabled: false },
          fontSize: 14,
          lineNumbers: 'on',
          wordWrap: 'on',
          scrollBeyondLastLine: false,
          automaticLayout: true,
          tabSize: 2,
          formatOnPaste: !isDetailXml,
          formatOnType: !isDetailXml,
          readOnly: isDetailXml,
        }}
      />
    </div>
  );
}
