'use client';

import { useEffect } from 'react';
import { initializeTheme } from '@/store/themeStore';

export default function ThemeProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    initializeTheme();
  }, []);

  return <>{children}</>;
}
