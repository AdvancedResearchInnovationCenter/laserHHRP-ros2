// Cross-section figures for the inspection mode cards on Setup: the plate in the card's text colour,
// the measured quantity in the info colour.
import type { Mode } from "../api/types";

// Plate cut through a countersunk hole: cone from y 40 to 60, then the straight bore.
const PLATE_LEFT = "M10 40 H90 L108 60 V100 H10 Z";
const PLATE_RIGHT = "M230 40 H150 L132 60 V100 H230 Z";

function Plate() {
  return (
    <g fill="currentColor" fillOpacity={0.16} stroke="currentColor" strokeWidth={2} strokeLinejoin="round">
      <path d={PLATE_LEFT} />
      <path d={PLATE_RIGHT} />
    </g>
  );
}

function Countersink() {
  return (
    <>
      <Plate />
      <g stroke="var(--info)" strokeWidth={2} fill="var(--info)">
        {/* depth: from the surface to the bottom of the cone */}
        <line x1={134} y1={60} x2={196} y2={60} strokeDasharray="4 3" />
        <line x1={186} y1={46} x2={186} y2={54} />
        <path d="M186 40 L182 47 H190 Z" stroke="none" />
        <path d="M186 60 L182 53 H190 Z" stroke="none" />
      </g>
    </>
  );
}

function Rivet() {
  return (
    <>
      <Plate />
      <g fill="var(--info)" fillOpacity={0.35} stroke="var(--info)" strokeWidth={2} strokeLinejoin="round">
        {/* countersunk rivet, head standing slightly proud of the surface */}
        <path d="M84 33 H156 L150 40 L132 60 V100 H108 V60 L90 40 Z" />
        <rect x={100} y={100} width={40} height={9} rx={3} />
      </g>
      <g stroke="var(--info)" strokeWidth={2} fill="var(--info)">
        {/* flushness: head top against the plate surface */}
        <line x1={156} y1={33} x2={204} y2={33} strokeDasharray="4 3" />
        <line x1={196} y1={16} x2={196} y2={26} />
        <line x1={196} y1={47} x2={196} y2={57} />
        <path d="M196 33 L192 26 H200 Z" stroke="none" />
        <path d="M196 40 L192 47 H200 Z" stroke="none" />
      </g>
    </>
  );
}

export function ModeFigure({ mode }: { mode: Mode }) {
  return (
    <svg className="mode-figure" viewBox="0 8 240 108" aria-hidden="true" data-testid={`figure-${mode}`}>
      {mode === "csk_depth" ? <Countersink /> : <Rivet />}
    </svg>
  );
}
