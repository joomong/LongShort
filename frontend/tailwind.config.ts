import type { Config } from "tailwindcss";
import defaultTheme from 'tailwindcss/defaultTheme';

const config: Config = {
  darkMode: 'class',
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['var(--font-sans)', ...defaultTheme.fontFamily.sans],
        mono: ['var(--font-mono)', ...defaultTheme.fontFamily.mono],
      },
      colors: {
        background: "var(--background)",
        "background-secondary": "var(--background-secondary)",
        "background-tertiary": "var(--background-tertiary)",
        "background-elevated": "var(--background-elevated)",
        foreground: "var(--foreground)",
        "foreground-secondary": "var(--foreground-secondary)",
        "foreground-muted": "var(--foreground-muted)",
        border: "var(--border)",
        "border-strong": "var(--border-strong)",
        "border-hover": "var(--border-hover)",
        accent: "var(--accent)",
        "accent-hover": "var(--accent-hover)",
        "chart-background": "var(--chart-background)",
        "chart-grid": "var(--chart-grid)",
        "chart-text": "var(--chart-text)",
        "chart-line": "var(--chart-line)",
        "row-hover": "var(--row-hover)",
        "metric-positive": "var(--metric-positive)",
        "metric-neutral": "var(--metric-neutral)",
        "metric-negative": "var(--metric-negative)",
        "row-active": "var(--row-active)",
        "data-color": "var(--data-color)",
        "metric-positive-bg": "var(--metric-positive-bg)",
        "metric-negative-bg": "var(--metric-negative-bg)",
        "metric-neutral-bg": "var(--metric-neutral-bg)",
      },
      boxShadow: {
        card: "0 1px 3px rgba(0,0,0,0.3)",
        elevated: "0 4px 12px rgba(0,0,0,0.4)",
        modal: "0 8px 32px rgba(0,0,0,0.5)",
      },
    },
  },
  plugins: [],
};
export default config;
