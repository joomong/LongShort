import { useEffect, useRef, useState, type RefObject } from 'react';
import uPlot from 'uplot';

export interface ContainerSize { width: number; height: number }

export function useContainerSize(ref: RefObject<HTMLElement>): ContainerSize {
  const [size, setSize] = useState<ContainerSize>({ width: 0, height: 0 });
  // Run after every commit so a conditionally rendered container is observed too.
  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    const measure = () => {
      const next = { width: Math.floor(container.clientWidth), height: Math.floor(container.clientHeight) };
      setSize(previous => previous.width === next.width && previous.height === next.height ? previous : next);
    };
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  });
  return size;
}

export function useUPlot(ref: RefObject<HTMLElement>, options: uPlot.Options | null, data: uPlot.AlignedData, size: ContainerSize) {
  const plot = useRef<uPlot | null>(null);
  const latestData = useRef(data);
  latestData.current = data;
  const active = useRef<{ container: HTMLElement; options: uPlot.Options } | null>(null);
  // Conditional empty states can replace the DOM node without changing a ref object.
  useEffect(() => {
    const container = ref.current;
    const ready = container && options && size.width > 0 && size.height > 0;
    if (!ready || active.current?.container !== container || active.current?.options !== options) {
      plot.current?.destroy();
      plot.current = null;
      active.current = null;
    }
    if (container && options && ready && !plot.current) {
      plot.current = new uPlot(options, latestData.current, container);
      active.current = { container, options };
    }
  });
  useEffect(() => () => {
    plot.current?.destroy();
    plot.current = null;
    active.current = null;
  }, []);
  useEffect(() => { plot.current?.setData(data); }, [data]);
  return plot;
}
