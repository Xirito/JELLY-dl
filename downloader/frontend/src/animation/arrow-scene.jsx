// Arrow state-cycle animation — the pointer head rotates, the tail bends like an Enter symbol.
import React from "react";
import "./animations-v3.jsx";
import "./tweaks-panel.jsx";

const { useComposition, CompositionStage, Easing, animate } = window;
const lerp = (k, a, b) => a + (b - a) * k;
const { useTweaks, TweaksPanel, TweakSection, TweakToggle, TweakSlider, TweakColor } = window;

const DEFAULT_TWEAKS = {
  motionEditor: false,
  showLabels: false,
  glow: 0.55,
  palette: ["#c26bff", "#08080d", "#171331", "#22c9ff"],
};

const DEFAULT_SCENES =
  '[{"name":"Rest","dur":0.9,"desc":"The pointer alone sits still, aimed right"},{"name":"Extend","dur":1.3,"desc":"A tail grows behind the pointer as it travels right"},{"name":"Turn","dur":1.4,"desc":"Only the pointer rotates a quarter turn clockwise, and the tail bends down into an Enter shape"},{"name":"Hold","dur":0.9,"desc":"The Enter shape holds: long horizontal run, shorter vertical drop"},{"name":"Retract","dur":1.1,"desc":"The tail retracts back into the pointer"},{"name":"Snap","dur":0.6,"desc":"The pointer swings back to the opening state"}]';

const DEFAULT_PLAYBACK = '{"mode":"loop"}';

const W = 1920, H = 1080;

const MOTION = {
  enter: (o) => animate({ ...o, ease: Easing.easeOutCubic }),
  draw: (o) => animate({ ...o, ease: Easing.easeInOutCubic }),
  pop: (o) => animate({ ...o, ease: Easing.easeOutBack }),
};

// geometry
const BASE_Y = 540;
const START_X = 700;    // pointer centre at rest
const BEND_X = 1180;    // where the tail turns downward
const TAIL_ORIGIN = 620; // left end of the horizontal run once extended
const DROP = 190;       // vertical run of the enter shape (shorter than the horizontal)
const HL = 120;         // pointer length
const HH = 92;          // pointer half-width
const PIVOT = 62;       // pointer centre, measured back from the tip

const HEAD = `M 0 0 L ${-HL} ${-HH} L ${-HL} ${HH} Z`;
const INNER = `M ${-46} 0 L ${-100} ${-34} L ${-100} ${34} Z`;

function tailPath(hx, hy, hlen, cornerY) {
  const startX = hx - hlen;
  const vertical = hy - cornerY > 1;
  const horizontal = hlen > 1;
  if (horizontal && vertical) return `M ${startX} ${cornerY} L ${hx} ${cornerY} L ${hx} ${hy}`;
  if (vertical) return `M ${hx} ${cornerY} L ${hx} ${hy}`;
  return `M ${startX} ${cornerY} L ${hx} ${cornerY}`;
}

function Arrow({ hx, hy, angle, hlen, cornerY, glow, ink }) {
  const tail = tailPath(hx, hy, hlen, cornerY);
  const headT = `translate(${hx} ${hy}) rotate(${angle}) translate(${PIVOT} 0)`;
  const visible = hlen > 2 || hy - cornerY > 2;
  const body = (w, paint) => (
    <g>
      {visible && (
        <path d={tail} fill="none" stroke={paint === "grad" ? "url(#tailGrad)" : paint} strokeWidth={w} strokeLinejoin="round" strokeLinecap="round" />
      )}
      <g transform={headT}>
        <path d={HEAD} fill={paint === "grad" ? "url(#headGrad)" : paint} stroke={paint === "grad" ? "url(#headGrad)" : paint} strokeWidth={w + 26} strokeLinejoin="round" strokeLinecap="round" />
      </g>
    </g>
  );
  return (
    <g>
      <g filter="url(#arrowGlow)" opacity={glow}>{body(96, "grad")}</g>
      {body(126, ink)}
      {body(100, "grad")}
      <g transform={headT}>
        <path d={INNER} fill={ink} stroke={ink} strokeWidth={18} strokeLinejoin="round" strokeLinecap="round" />
      </g>
    </g>
  );
}

function Piece({ tw, background }) {
  const { T, CUES, authoredTotal } = useComposition();
  const ink = "#0b0b12";

  // pointer x: at rest, then travels right while the tail grows behind it
  const hx =
    T < CUES.Extend ? START_X
    : T < CUES.Turn ? MOTION.draw({ from: START_X, to: BEND_X, start: CUES.Extend, end: CUES.Turn })(T)
    : T < CUES.Snap ? BEND_X
    : MOTION.draw({ from: BEND_X, to: START_X, start: CUES.Snap, end: authoredTotal })(T);

  // head rotation: only the pointer turns
  const angle =
    T < CUES.Turn ? 0
    : T < CUES.Snap ? MOTION.draw({ from: 0, to: 90, start: CUES.Turn + 0.1, end: CUES.Turn + 1.0 })(T)
    : MOTION.enter({ from: 90, to: 0, start: CUES.Snap + 0.05, end: authoredTotal - 0.05 })(T);

  // the pointer noses down as it turns (arc, like a car), then drops the rest once square
  const arc = DROP * 0.42 * (1 - Math.cos((angle * Math.PI) / 180));
  const settle = MOTION.enter({ from: 0, to: DROP * 0.58, start: CUES.Turn + 1.0, end: CUES.Turn + 1.45 })(T);
  const drop =
    T < CUES.Turn ? 0
    : T < CUES.Snap ? arc + settle
    : MOTION.draw({ from: DROP, to: 0, start: CUES.Snap, end: authoredTotal })(T);

  // horizontal run retracts first, from its far end; the pointer stays put
  const hlen =
    T < CUES.Extend ? 0
    : T < CUES.Retract ? MOTION.draw({ from: 0, to: BEND_X - TAIL_ORIGIN, start: CUES.Extend, end: CUES.Turn })(T)
    : T < CUES.Snap ? MOTION.draw({ from: BEND_X - TAIL_ORIGIN, to: 0, start: CUES.Retract, end: CUES.Retract + 0.6 })(T)
    : 0;

  const hy = BASE_Y + drop;
  // then the vertical run shortens from the top, sliding into the stationary pointer
  const cornerY =
    T < CUES.Retract ? BASE_Y
    : T < CUES.Snap ? MOTION.draw({ from: BASE_Y, to: hy, start: CUES.Retract + 0.5, end: CUES.Snap })(T)
    : hy;

  const label =
    T < CUES.Extend ? "01 · pointer" :
    T < CUES.Turn ? "02 · extend" :
    T < CUES.Hold ? "03 · quarter turn" :
    T < CUES.Retract ? "04 · enter" :
    T < CUES.Snap ? "05 · retract" : "06 · snap back";

  const hasBackground = background !== "transparent";

  return (
    <div style={{ position: "absolute", inset: 0, background, overflow: "hidden" }}>
      {hasBackground && (
        <div style={{
          position: "absolute", inset: 0,
          background: `radial-gradient(60% 60% at 50% 45%, ${tw.palette[2]} 0%, rgba(0,0,0,0) 70%)`,
          opacity: 0.5,
        }} />
      )}
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ position: "absolute", inset: 0 }}>
        <defs>
          <linearGradient id="tailGrad" gradientUnits="userSpaceOnUse" x1="480" y1="500" x2="1230" y2="700">
            <stop offset="0" stopColor={tw.palette[0]} />
            <stop offset="1" stopColor="#8b6bff" />
          </linearGradient>
          <linearGradient id="headGrad" gradientUnits="userSpaceOnUse" x1={-HL - 30} y1="0" x2="10" y2="0">
            <stop offset="0" stopColor="#8b6bff" />
            <stop offset="1" stopColor={tw.palette[3]} />
          </linearGradient>
          <filter id="arrowGlow" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="34" />
          </filter>
        </defs>
        <Arrow hx={hx} hy={hy} angle={angle} hlen={hlen} cornerY={cornerY} glow={tw.glow} ink={ink} />
      </svg>
      {tw.showLabels && (
        <div style={{
          position: "absolute", left: 72, bottom: 64,
          font: "500 30px/1 ui-monospace, SFMono-Regular, Menlo, monospace",
          letterSpacing: "0.08em", color: "rgba(255,255,255,0.42)",
        }}>{label}</div>
      )}
    </div>
  );
}

export default function ArrowVideo({ showControls = true } = {}) {
  const [tw, setTweak] = useTweaks(window.TWEAK_DEFAULTS ?? DEFAULT_TWEAKS);
  const background = showControls ? tw.palette[1] : "transparent";
  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <CompositionStage width={W} height={H} scenes={window.OM_SCENES ?? DEFAULT_SCENES} playback={window.OM_PLAYBACK ?? DEFAULT_PLAYBACK} bg={background} showPlaybackBar={showControls}>
        <Piece tw={tw} background={background} />
      </CompositionStage>
      {showControls && (
        <TweaksPanel>
          <TweakSection label="Playback" />
          <TweakToggle label="Motion editor" value={tw.motionEditor} onChange={(v) => setTweak("motionEditor", v)} />
          <TweakToggle label="State labels" value={tw.showLabels} onChange={(v) => setTweak("showLabels", v)} />
          <TweakSection label="Look" />
          <TweakSlider label="Glow" value={tw.glow} min={0} max={1} step={0.05} onChange={(v) => setTweak("glow", v)} />
          <TweakColor
            label="Palette"
            value={tw.palette}
            options={[
              ["#c26bff", "#08080d", "#171331", "#22c9ff"],
              ["#ff8a5b", "#0b0908", "#2a1710", "#ffd166"],
              ["#4ade80", "#050a08", "#0d2a1c", "#22d3ee"],
            ]}
            onChange={(v) => setTweak("palette", v)}
          />
        </TweaksPanel>
      )}
    </div>
  );
}

window.ArrowVideo = ArrowVideo;
