import type uPlot from 'uplot';
import { fmtDate } from './uplotUtils';

export function clampRange(min: number, max: number, lo: number, hi: number) {
  if (![min, max, lo, hi].every(Number.isFinite) || hi <= lo) return null;
  const width = Math.min(Math.max(max - min, Number.EPSILON), hi - lo);
  const start = Math.max(lo, Math.min(min, hi - width));
  return { min: start, max: start + width };
}

export function wheelPixels(delta: number, mode: number, height: number) {
  return delta * (mode === 1 ? 16 : mode === 2 ? height : 1);
}

// Shared by signal and performance charts. All transient UI stays outside React.
export function chartInteraction(format: (value: number) => string, navigation = true): uPlot.Plugin {
  let dispose = () => {};
  let update = () => {};
  let hideOverlays = () => {};
  return { hooks: {
    ready: [(u: uPlot) => {
      const over = u.over;
      over.style.cursor = navigation ? 'grab' : 'crosshair';
      over.style.touchAction = 'pan-y';
      over.title = navigation ? 'Drag to pan · Scroll to zoom · Double-click to reset' : '';
      const tooltip = document.createElement('div');
      tooltip.className = 'chart-tooltip';
      tooltip.setAttribute('role', 'tooltip');
      const badge = document.createElement('div');
      badge.className = 'chart-cursor-date';
      Object.assign(tooltip.style, { position: 'fixed', pointerEvents: 'none', display: 'none', zIndex: '1000', padding: '6px 8px', font: '11px monospace', whiteSpace: 'pre', background: 'var(--background-elevated)', color: 'var(--foreground)', border: '1px solid var(--border-strong)' });
      Object.assign(badge.style, { position: 'absolute', pointerEvents: 'none', display: 'none', zIndex: '10', padding: '2px 4px', font: '11px monospace', whiteSpace: 'nowrap', background: 'var(--foreground)', color: 'var(--background)' });
      document.body.appendChild(tooltip);
      over.appendChild(badge);
      let pointer: { x: number; y: number } | null = null;
      let drag: { id: number; x: number; min: number; max: number; width: number } | null = null;
      let frame = 0;
      let pending: { min: number; max: number } | null = null;
      let tooltipWidth = 0;
      let tooltipHeight = 0;
      let badgeWidth = 0;
      const hide = () => { tooltip.style.display = 'none'; badge.style.display = 'none'; };
      hideOverlays = hide;
      const bounds = () => [u.data[0][0], u.data[0][u.data[0].length - 1]];
      const schedule = () => {
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = 0;
          if (pending) u.setScale('x', pending);
          pending = null;
          update();
        });
      };
      const apply = (min: number, max: number) => {
        const [lo, hi] = bounds();
        pending = clampRange(min, max, lo, hi);
        if (pending) schedule();
      };
      update = () => {
        if (!pointer || !u.data[0].length) { hide(); return; }
        const rect = over.getBoundingClientRect();
        const x = pointer.x - rect.left;
        if (x < 0 || x > rect.width || pointer.y < rect.top || pointer.y > rect.bottom) { hide(); return; }
        // Use the nearest timestamp, even when every series value is null.
        const idx = u.posToIdx(x);
        const date = fmtDate(u.data[0][idx]);
        if (!date) { hide(); return; }
        const lines = [date];
        for (let s = 1; s < u.series.length; s++) {
          if (u.series[s].show === false) continue;
          const value = u.data[s]?.[idx];
          lines.push(`${u.series[s].label ?? 'Value'}: ${typeof value === 'number' && Number.isFinite(value) ? format(value) : 'N/A'}`);
        }
        const text = lines.join('\n');
        tooltip.style.display = 'block';
        badge.style.display = 'block';
        if (tooltip.textContent !== text) {
          tooltip.textContent = text;
          tooltipWidth = tooltip.offsetWidth;
          tooltipHeight = tooltip.offsetHeight;
        }
        if (badge.textContent !== date) {
          badge.textContent = date;
          badgeWidth = badge.offsetWidth;
        }
        tooltip.style.left = `${Math.max(4, Math.min(pointer.x + 14, window.innerWidth - tooltipWidth - 4))}px`;
        tooltip.style.top = `${Math.max(4, Math.min(pointer.y + 14, window.innerHeight - tooltipHeight - 4))}px`;
        badge.style.top = `${rect.height + 2}px`;
        badge.style.left = `${Math.max(0, Math.min(x - badgeWidth / 2, rect.width - badgeWidth))}px`;
      };
      const down = (e: PointerEvent) => {
        if (!navigation || e.button !== 0 || u.data[0].length < 2) return;
        e.preventDefault();
        drag = { id: e.pointerId, x: e.clientX, min: u.scales.x.min!, max: u.scales.x.max!, width: over.clientWidth };
        over.setPointerCapture(e.pointerId);
        over.style.cursor = 'grabbing';
      };
      const move = (e: PointerEvent) => {
        pointer = { x: e.clientX, y: e.clientY };
        if (drag && drag.id === e.pointerId && drag.width > 0) {
          const shift = (e.clientX - drag.x) / drag.width * (drag.max - drag.min);
          apply(drag.min - shift, drag.max - shift);
        }
        schedule();
      };
      const up = (e: PointerEvent) => {
        drag = null;
        if (over.hasPointerCapture(e.pointerId)) over.releasePointerCapture(e.pointerId);
        over.style.cursor = navigation ? 'grab' : 'crosshair';
      };
      const leave = () => { if (!drag) { pointer = null; hide(); } };
      const reset = () => { if (navigation) { const [lo, hi] = bounds(); apply(lo, hi); } };
      const wheel = (e: WheelEvent) => {
        if (!navigation || u.data[0].length < 2) return;
        e.preventDefault();
        const min = pending?.min ?? u.scales.x.min!;
        const max = pending?.max ?? u.scales.x.max!;
        const rect = over.getBoundingClientRect();
        const dx = wheelPixels(e.deltaX, e.deltaMode, rect.height);
        const dy = wheelPixels(e.deltaY, e.deltaMode, rect.height);
        if (Math.abs(dx) > Math.abs(dy) && !e.ctrlKey) {
          const shift = dx / rect.width * (max - min);
          apply(min + shift, max + shift);
        } else {
          const [lo, hi] = bounds();
          const span = max - min;
          const t = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
          const minimum = Math.min(hi - lo, Math.max(1, u.data[0][1] - lo) * 5);
          const width = Math.max(minimum, Math.min(hi - lo, span * Math.exp(Math.max(-4, Math.min(4, dy * 0.002)))));
          const anchor = min + span * t;
          apply(anchor - width * t, anchor + width * (1 - t));
        }
      };
      over.addEventListener('pointerdown', down);
      over.addEventListener('pointermove', move);
      over.addEventListener('pointerup', up);
      over.addEventListener('pointercancel', up);
      over.addEventListener('lostpointercapture', up);
      over.addEventListener('pointerleave', leave);
      over.addEventListener('wheel', wheel, { passive: false });
      over.addEventListener('dblclick', reset);
      window.addEventListener('scroll', hide, true);
      dispose = () => {
        cancelAnimationFrame(frame);
        over.removeEventListener('pointerdown', down);
        over.removeEventListener('pointermove', move);
        over.removeEventListener('pointerup', up);
        over.removeEventListener('pointercancel', up);
        over.removeEventListener('lostpointercapture', up);
        over.removeEventListener('pointerleave', leave);
        over.removeEventListener('wheel', wheel);
        over.removeEventListener('dblclick', reset);
        window.removeEventListener('scroll', hide, true);
        tooltip.remove(); badge.remove();
      };
    }],
    setSize: [() => hideOverlays()],
    setData: [() => hideOverlays()],
    destroy: [() => dispose()],
  } };
}
