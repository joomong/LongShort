'use client';

import { useMemo, useCallback, useEffect, useRef } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';
import dynamic from 'next/dynamic';

// Monaco Editor is client-only, so we need to dynamically import it
const Editor = dynamic(() => import('@monaco-editor/react'), { ssr: false });

export default function XmlEditor() {
  const xmlCode = useAlphaStore(s => s.xmlCode);
  const setXmlCode = useAlphaStore(s => s.setXmlCode);
  const viewMode = useAlphaStore(s => s.viewMode);
  const selectedAlphaIndex = useAlphaStore(s => s.selectedAlphaIndex);
  const result = useAlphaStore(s => s.result);
  const { theme } = useThemeStore();

  const xmlCodeRef = useRef(xmlCode);
  useEffect(() => {
    xmlCodeRef.current = xmlCode;
  }, [xmlCode]);

  const selectedAlphaXml = useMemo(() => {
    if (viewMode !== 'detail') return null;
    if (selectedAlphaIndex === null) return null;
    const alpha = selectedAlphaIndex === -1 ? result?.combined : result?.alphas?.[selectedAlphaIndex];
    return alpha?.xml ?? null;
  }, [viewMode, selectedAlphaIndex, result]);

  const isDetailXml = Boolean(selectedAlphaXml);
  const value = selectedAlphaXml ?? xmlCode;

  const editorOptions = useMemo(() => ({
    minimap: { enabled: false },
    fontSize: 14,
    lineNumbers: 'on' as const,
    wordWrap: 'on' as const,
    scrollBeyondLastLine: false,
    automaticLayout: true,
    tabSize: 2,
    formatOnPaste: !isDetailXml,
    formatOnType: !isDetailXml,
    readOnly: isDetailXml,
  }), [isDetailXml]);

  const handleChange = useCallback((v?: string) => {
    // In detail mode, show per-alpha XML read-only (do not overwrite main editor contents).
    if (isDetailXml) return;
    const next = v || '';
    if (next === xmlCodeRef.current) return;
    setXmlCode(next);
  }, [isDetailXml, setXmlCode]);

  return (
    <div className="flex-1 w-full h-full">
      <Editor
        height="100%"
        defaultLanguage="xml"
        value={value}
        onChange={handleChange}
        theme={theme === 'dark' ? 'vs-dark' : 'light'}
        options={editorOptions}
      />
    </div>
  );
}
