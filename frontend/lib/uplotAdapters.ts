import { useEffect, useRef, useState, type RefObject } from 'react';
import uPlot from 'uplot';

export interface ContainerSize { width: number; height: number }

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

// Options describe structure/theme only. Dimensions must not be memo dependencies.
export function useUPlot(ref: RefObject<HTMLElement>, options: uPlot.Options | null, data: uPlot.AlignedData, size: ContainerSize, resetKey?: unknown) {
  const plot = useRef<uPlot | null>(null);
  const active = useRef<{ container: HTMLElement; options: uPlot.Options } | null>(null);
  const previous = useRef<{ data: uPlot.AlignedData; key: unknown } | null>(null);
  useEffect(() => {
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
      plot.current = new uPlot({ ...options, width: size.width, height: size.height }, data, container);
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
