import { useEffect, useLayoutEffect, useRef, useState } from "react";

// no layout effects outside a browser (server rendering in the component tests)
const useIsoLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

/** Measured content size of an element (for SVG maps and circle grids); defaults before layout / in tests. */
export function useBoxSize<T extends HTMLElement>(w0: number, h0: number) {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: w0, h: h0 });
  useIsoLayoutEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const w = Math.floor(el.clientWidth), h = Math.floor(el.clientHeight);
      if (w > 0 && h > 0) setSize((s) => (s.w === w && s.h === h ? s : { w, h }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size.w, size.h] as const;
}
