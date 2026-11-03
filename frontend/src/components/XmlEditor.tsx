'use client';

import { useMemo, useCallback, useEffect, useRef } from 'react';
import { useRunStore } from '@/runStore';
import { useThemeStore } from '@/theme';
import dynamic from 'next/dynamic';

// Monaco Editor is client-only, so we need to dynamically import it
const Editor = dynamic(async () => {
  const monacoReact = await import('@monaco-editor/react');
  monacoReact.loader.config({ paths: { vs: '/monaco/vs' } });
  return monacoReact.default;
}, { ssr: false });

export default function XmlEditor() {
  const xmlCode = useRunStore(s => s.xmlCode);
  const setXmlCode = useRunStore(s => s.setXmlCode);
  const { theme } = useThemeStore();

  const xmlCodeRef = useRef(xmlCode);
  useEffect(() => {
    xmlCodeRef.current = xmlCode;
  }, [xmlCode]);

  const editorOptions = useMemo(() => ({
    minimap: { enabled: false },
    fontSize: 12,
    lineHeight: 16,
    lineNumbersMinChars: 3,
    glyphMargin: false,
    lineDecorationsWidth: 4,
    padding: { top: 8, bottom: 8 },
    lineNumbers: 'on' as const,
    wordWrap: 'on' as const,
    scrollBeyondLastLine: false,
    automaticLayout: true,
    tabSize: 2,
    formatOnPaste: true,
    formatOnType: true,
    readOnly: false,
  }), []);

  const handleChange = useCallback((v?: string) => {
    const next = v || '';
    if (next === xmlCodeRef.current) return;
    setXmlCode(next);
  }, [setXmlCode]);

  return (
    <div className="flex-1 min-h-0 w-full h-full">
      <Editor
        height="100%"
        defaultLanguage="xml"
        value={xmlCode}
        onChange={handleChange}
        theme={theme === 'dark' ? 'vs-dark' : 'light'}
        options={editorOptions}
      />
    </div>
  );
}
