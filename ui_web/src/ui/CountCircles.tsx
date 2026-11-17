import { circleModel, circleSize } from "../lib/circles";

export function CountCircles({ expected, detected, targets, width, height }: {
  expected: number | null; detected: number; targets: { partial: boolean }[]; width: number; height: number;
}) {
  const m = circleModel(expected, detected, targets);
  const size = circleSize(m.circles.length + (m.overflow ? 2 : 0), width, height);
  if (m.total === 0) return <div className="circles-empty" style={{ width, height }}>No features detected yet</div>;
  return (
    <div className="circles" style={{ width, maxHeight: height }} data-testid="circles">
      {m.circles.map((c, i) => (
        <span key={i} className={`circle ${c}`} style={{ width: size, height: size }} data-state={c} />
      ))}
      {m.overflow > 0 && <span className="circle-more" style={{ height: size }}>+{m.overflow}</span>}
    </div>
  );
}
