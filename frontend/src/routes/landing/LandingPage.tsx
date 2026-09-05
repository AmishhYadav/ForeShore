/**
 * Landing page with scroll-animated storytelling sections.
 *
 * This is a pure presentation component with no backend calls — it exists
 * solely to sell the project before the user enters the tool surfaces at
 * /boat or /console. IntersectionObserver drives the scroll animations
 * so there's no dependency on a scroll-animation library.
 */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import "./landing.css";

function useScrollVisibility(threshold = 0.15) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          observer.unobserve(el);
        }
      },
      { threshold },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [threshold]);
  return { ref, visible };
}

function WaveSVG() {
  return (
    <div className="hero__waves">
      <svg viewBox="0 0 2880 320" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
        <path
          d="M0,160 C480,280 960,40 1440,160 C1920,280 2400,40 2880,160 L2880,320 L0,320 Z"
          fill="rgba(6, 19, 31, 0.6)"
        />
        <path
          d="M0,200 C480,300 960,100 1440,200 C1920,300 2400,100 2880,200 L2880,320 L0,320 Z"
          fill="rgba(6, 19, 31, 0.8)"
        />
        <path
          d="M0,240 C480,300 960,180 1440,240 C1920,300 2400,180 2880,240 L2880,320 L0,320 Z"
          fill="#06131f"
        />
      </svg>
    </div>
  );
}

function Bubbles() {
  return (
    <div className="hero__bubbles">
      {Array.from({ length: 10 }, (_, i) => (
        <div key={i} className="hero__bubble" />
      ))}
    </div>
  );
}

export default function LandingPage() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    function onScroll() {
      setScrolled(window.scrollY > 60);
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const s1 = useScrollVisibility();
  const s2 = useScrollVisibility();
  const s3 = useScrollVisibility();
  const s4 = useScrollVisibility();
  const s5 = useScrollVisibility();

  return (
    <div className="landing">
      {/* ── Navigation ──────────────────────────────────────────── */}
      <nav className={`landing-nav${scrolled ? " landing-nav--scrolled" : ""}`}>
        <div className="landing-nav__logo">
          <span className="landing-nav__logo-mark" />
          FORESHORE
        </div>
        <ul className="landing-nav__links">
          <li><a className="landing-nav__link" href="#about">About</a></li>
          <li><Link className="landing-nav__link" to="/boat">Boat UI</Link></li>
          <li><Link className="landing-nav__link" to="/console">Console</Link></li>
        </ul>
      </nav>

      {/* ── Hero ────────────────────────────────────────────────── */}
      <section className="hero">
        <div className="hero__bg" />
        <div className="hero__glow" />
        <Bubbles />
        <WaveSVG />

        <div className="hero__content">
          <div className="hero__badge">
            <span className="hero__badge-dot" />
            SIH 2026 · Problem Statement SIH26176 · ISRO / DoS
          </div>
          <h1 className="hero__title">FORESHORE</h1>
          <p className="hero__tagline">Marine foresight for the small-boat fleet</p>
          <p className="hero__motto">Reasoning ashore. Decision aboard.</p>
          <div className="hero__actions">
            <Link to="/boat" className="hero__cta hero__cta--primary">
              Launch Tool →
            </Link>
            <a href="#about" className="hero__cta hero__cta--ghost">
              Learn More ↓
            </a>
          </div>
        </div>
      </section>

      {/* ── Section 1: The Problem ──────────────────────────────── */}
      <section id="about" className="story-section">
        <div className="story-section__bg" />
        <div ref={s1.ref} className={`story-section__content${s1.visible ? " visible" : ""}`}>
          <div className="story-section__label">The Problem</div>
          <h2 className="story-section__title">The Communication Gap That Costs Lives</h2>
          <p className="story-section__body">
            Fishing boats operate 100–150 km offshore. Mobile and VHF radio die at 10–20 km.
            When Cyclone Ockhi struck in 2017, IMD issued warnings with 48 hours of lead time — but
            they never reached the boats already at sea. The lead time held no relevance for
            fishermen who couldn&apos;t be contacted.
          </p>
          <div className="stat-compare">
            <div className="stat-card stat-card--danger">
              <div className="stat-card__value">10–20 km</div>
              <div className="stat-card__label">Mobile / VHF radio range</div>
            </div>
            <div className="stat-card">
              <div className="stat-card__value">100–150 km</div>
              <div className="stat-card__label">Fishing ground distance from shore</div>
            </div>
            <div className="stat-card stat-card--danger">
              <div className="stat-card__value">529+</div>
              <div className="stat-card__label">Indian fishermen arrested for drifting across IMBL (2024)</div>
            </div>
          </div>
        </div>
      </section>

      {/* ── Section 2: Our Solution ─────────────────────────────── */}
      <section className="story-section">
        <div className="story-section__bg" />
        <div ref={s2.ref} className={`story-section__content${s2.visible ? " visible" : ""}`}>
          <div className="story-section__label">Our Solution</div>
          <h2 className="story-section__title">Every System Tells You What the Sea Is Doing. We Tell You What You Should Do.</h2>
          <p className="story-section__body">
            FORESHORE is an agentic marine intelligence platform. Ten AI specialists collaborate to
            answer any marine safety question — from "is it safe to go fishing?" to "plan a route
            avoiding the IMBL." The system gives a verdict with full evidence and explainability.
            The LLM selects and sequences tools — but never does the arithmetic.
          </p>
          <div className="verdict-demo">
            <div className="verdict-demo__card">
              <div className="verdict-demo__icon verdict-demo__icon--go" />
              <div className="verdict-demo__level" style={{ color: "var(--verdict-go)" }}>GO</div>
              <div className="verdict-demo__desc">Conditions are favorable. Safe to proceed with normal precautions.</div>
            </div>
            <div className="verdict-demo__card">
              <div className="verdict-demo__icon verdict-demo__icon--caution" />
              <div className="verdict-demo__level" style={{ color: "var(--verdict-caution)" }}>GO WITH CAUTION</div>
              <div className="verdict-demo__desc">Conditions are marginal. Proceed with heightened awareness and shorter trips.</div>
            </div>
            <div className="verdict-demo__card">
              <div className="verdict-demo__icon verdict-demo__icon--stop" />
              <div className="verdict-demo__level" style={{ color: "var(--verdict-stop)" }}>DO NOT ADVISE</div>
              <div className="verdict-demo__desc">Conditions exceed safe thresholds. Contact nearest authority for guidance.</div>
            </div>
          </div>
        </div>
      </section>

      {/* ── Section 3: How It Works ─────────────────────────────── */}
      <section className="story-section">
        <div className="story-section__bg" />
        <div ref={s3.ref} className={`story-section__content${s3.visible ? " visible" : ""}`}>
          <div className="story-section__label">How It Works</div>
          <h2 className="story-section__title">From Question to Verdict in Seconds</h2>
          <p className="story-section__body">
            A single question triggers a full agentic reasoning pipeline. The planner decomposes
            the query, dispatches tool calls to retrieve live data from IMD, INCOIS, Open-Meteo and
            GDACS, runs a deterministic verdict engine, applies the advisory ceiling, and synthesises
            a spoken response — all with full provenance.
          </p>
          <div className="pipeline">
            {[
              "Query", "Language Detection", "Planning", "Tool Calls",
              "Verdict Engine", "Advisory Ceiling", "Synthesis", "Answer"
            ].map((step, i, arr) => (
              <div className="pipeline__step" key={step}>
                <div className="pipeline__node">{step}</div>
                {i < arr.length - 1 && <span className="pipeline__arrow">→</span>}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Section 4: The Innovation ───────────────────────────── */}
      <section className="story-section">
        <div className="story-section__bg" />
        <div ref={s4.ref} className={`story-section__content${s4.visible ? " visible" : ""}`}>
          <div className="story-section__label">The Innovation</div>
          <h2 className="story-section__title">220 Bits That Cross the Satellite</h2>
          <p className="story-section__body">
            FORESHORE compresses its full agentic reasoning — every source, every threshold, the
            binding constraint and the handoff — into a 220-bit payload that fits ISRO's existing
            NavIC satellite sub-frame unchanged. Same ICD, same bit budget, same 12-second cadence.
            The expensive reasoning happens ashore. What crosses the satellite link is the
            <em> decision</em>, not the data.
          </p>
          <div className="bit-layout">
            <div className="bit-layout__segment" style={{ flex: 0.3 }}>
              TLM<span className="bit-layout__segment-label">8 bits</span>
            </div>
            <div className="bit-layout__segment" style={{ flex: 0.6 }}>
              TOWC<span className="bit-layout__segment-label">17 bits</span>
            </div>
            <div className="bit-layout__segment" style={{ flex: 0.2 }}>
              RSVD<span className="bit-layout__segment-label">5 bits</span>
            </div>
            <div className="bit-layout__segment" style={{ flex: 0.2 }}>
              MSG ID<span className="bit-layout__segment-label">6 bits</span>
            </div>
            <div className="bit-layout__segment bit-layout__segment--highlight">
              DECISION PAYLOAD<span className="bit-layout__segment-label">220 bits — verdict + binding constraint + margin + envelope</span>
            </div>
            <div className="bit-layout__segment" style={{ flex: 0.2 }}>
              RSVD<span className="bit-layout__segment-label">6 bits</span>
            </div>
            <div className="bit-layout__segment" style={{ flex: 0.8 }}>
              CRC<span className="bit-layout__segment-label">24 bits</span>
            </div>
          </div>
        </div>
      </section>

      {/* ── Section 5: Two Surfaces ─────────────────────────────── */}
      <section className="story-section">
        <div className="story-section__bg" />
        <div ref={s5.ref} className={`story-section__content${s5.visible ? " visible" : ""}`}>
          <div className="story-section__label">Two Surfaces, One Core</div>
          <h2 className="story-section__title">Built for Those Who Need It</h2>
          <p className="story-section__body">
            Both surfaces call the exact same <code>POST /api/query</code> endpoint — one agent core,
            two renderers. Only the <code>surface</code> field differs, which selects language default
            and copy, never a different reasoning path.
          </p>
          <div className="surfaces-grid">
            <div className="surface-card">
              <div className="surface-card__icon">🚤</div>
              <h3 className="surface-card__title">Boat UI</h3>
              <p className="surface-card__body">
                Tamil-first, voice-first, verdict-first. Built for a fisherman at sea at night
                with wet hands. Large touch targets, spoken responses, cached verdicts for when
                signal drops.
              </p>
              <Link to="/boat" className="surface-card__cta">
                Open Boat UI →
              </Link>
            </div>
            <div className="surface-card">
              <div className="surface-card__icon">🖥️</div>
              <h3 className="surface-card__title">Shore Console</h3>
              <p className="surface-card__body">
                English, information-dense. Built for the fisheries officer or Coast Guard operator
                watching a whole simulated fleet through a cyclone. Fleet map, alert queue,
                reasoning traces.
              </p>
              <Link to="/console" className="surface-card__cta">
                Open Console →
              </Link>
            </div>
          </div>
        </div>
      </section>

      {/* ── Footer ──────────────────────────────────────────────── */}
      <footer className="landing-footer">
        <div className="landing-footer__brand">FORESHORE</div>
        <hr className="landing-footer__divider" />
        <p className="landing-footer__text">
          Built for Smart India Hackathon 2026
        </p>
        <p className="landing-footer__text">
          Problem Statement SIH26176 &ldquo;ORCA&rdquo; — Indian Space Research Organisation / Department of Space
        </p>
        <p className="landing-footer__text" style={{ marginTop: "var(--space-4)", opacity: 0.5 }}>
          Marine EcOsystem Reasoning with Collaborative Agents
        </p>
      </footer>
    </div>
  );
}
