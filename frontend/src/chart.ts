import { useEffect, useRef, useState, type RefObject } from 'react';
import uPlot from 'uplot';
import type { ThemeColors } from './chartStyle';
import { formatDate } from './format';

const fmtDate = (seconds: number) => formatDate(seconds * 1000);
function timeAxisValues(plot: uPlot, values: number[]): string[] {
  const span = (plot.scales.x.max ?? 0) - (plot.scales.x.min ?? 0);
  return values.map(value => span > 86400 * 365 * 4 ? fmtDate(value).slice(0, 4) : span > 86400 * 120 ? fmtDate(value).slice(0, 7) : fmtDate(value));
}
export function buildUPlotAxes(colors: ThemeColors): [uPlot.Axis, uPlot.Axis] {
  const shared: uPlot.Axis = { stroke: colors.chartText, font: '11px monospace', grid: { stroke: colors.chartGrid, width: 1 }, ticks: { stroke: colors.chartGrid, width: 1 } };
  return [{ ...shared, values: timeAxisValues, space: 100, size: 44 }, { ...shared, size: 55 }];
}
export function alignedSeries(timestamps: readonly number[], columns: readonly (number | null)[][]): uPlot.AlignedData {
  return [timestamps.map(time => time / 1000), ...columns];
}
export function isMember(membership: string[] | undefined, row: number, column: number): boolean {
  return membership?.[row]?.[column] !== '0';
}
export function splitByMembership(values: (number | null)[], members: boolean[]) {
  const inside = values.map((value, index) => members[index] === false ? null : value);
  const outside = values.map((value, index) => {
    const out = (i: number) => i >= 0 && i < values.length && members[i] === false;
    return out(index) || out(index - 1) || out(index + 1) ? value : null;
  });
  return { inside, outside };
}


interface ContainerSize { width: number; height: number }

export function useContainerSize(ref: RefObject<HTMLElement>): ContainerSize {
  const [size, setSize] = useState<ContainerSize>({ width: 0, height: 0 });
  const watched = useRef<HTMLElement | null>(null);
  const cleanup = useRef<(() => void) | null>(null);
  // Observe replacements, but do not disconnect/re-measure on every render.
  useEffect(() => {
    const container = ref.current;
    if (watched.current === container) return;
    cleanup.current?.();
    watched.current = container;
    if (!container) return;
    let frame = 0;
    const measure = () => {
      const cs = getComputedStyle(container);
      const next = {
        width: Math.max(0, Math.floor(container.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight))),
        height: Math.max(0, Math.floor(container.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom))),
      };
      setSize(previous => previous.width === next.width && previous.height === next.height ? previous : next);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure); };
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', schedule);
      cleanup.current = () => { cancelAnimationFrame(frame); window.removeEventListener('resize', schedule); };
    } else {
      const observer = new ResizeObserver(schedule);
      observer.observe(container);
      cleanup.current = () => { cancelAnimationFrame(frame); observer.disconnect(); };
    }
  });
  useEffect(() => () => { cleanup.current?.(); watched.current = null; }, []);
  return size;
}

// Timestamps are UTC midnights and labels format in UTC; place time-axis splits in UTC too,
// otherwise the browser zone shifts ticks (KST: 2020-01-01 tick lands on 2019-12-31T15:00Z).
const utcDate = (ts: number): Date => uPlot.tzDate(new Date(ts * 1e3), 'Etc/UTC');

// Options describe structure/theme only. Dimensions must not be memo dependencies.
export function useUPlot(ref: RefObject<HTMLElement>, options: uPlot.Options | null, data: uPlot.AlignedData, size: ContainerSize, resetKey?: unknown, enabled = true) {
  const plot = useRef<uPlot | null>(null);
  const active = useRef<{ container: HTMLElement; options: uPlot.Options } | null>(null);
  const previous = useRef<{ data: uPlot.AlignedData; key: unknown } | null>(null);
  useEffect(() => {
    if (!enabled) { plot.current?.over.dispatchEvent(new Event('pointerleave')); return; }
    const container = ref.current;
    const ready = container && options && size.width > 0 && size.height > 0;
    const old = plot.current;
    const sameKey = previous.current?.key === resetKey;
    const range = sameKey && old ? { min: old.scales.x.min!, max: old.scales.x.max! } : null;
    if (!container || !options || active.current?.container !== container || active.current?.options !== options) {
      plot.current?.destroy();
      plot.current = null;
      active.current = null;
    }
    const created = ready && !plot.current;
    if (container && options && created) {
      plot.current = new uPlot({ tzDate: utcDate, ...options, width: size.width, height: size.height }, data, container);
      active.current = { container, options };
    } else if (plot.current && ready && (plot.current.width !== size.width || plot.current.height !== size.height)) {
      plot.current.setSize(size);
    }
    if (plot.current) {
      if (!created && (previous.current?.data !== data || !sameKey)) plot.current.setData(data, !sameKey);
      if ((created || previous.current?.data !== data) && sameKey && range && Number.isFinite(range.min) && Number.isFinite(range.max)) {
        plot.current.setScale('x', range);
      }
      previous.current = { data, key: resetKey };
    }
  });
  useEffect(() => () => {
    plot.current?.destroy();
    plot.current = null;
    active.current = null;
    previous.current = null;
  }, []);
  return plot;
}


function clampRange(min: number, max: number, lo: number, hi: number) {
  if (![min, max, lo, hi].every(Number.isFinite) || hi <= lo) return null;
  const width = Math.min(Math.max(max - min, Number.EPSILON), hi - lo);
  const start = Math.max(lo, Math.min(min, hi - width));
  return { min: start, max: start + width };
}

function wheelPixels(delta: number, mode: number, height: number) {
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
