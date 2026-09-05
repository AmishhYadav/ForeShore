/**
 * Foreshore Landing Page
 *
 * Exact implementation of the complete landing page with WebGL Lightning
 * background, dynamic scroll reactions, section snap-scrolling, reveal masks,
 * glass cards, bit allocation diagram, and interactive routing.
 *
 * Sized to fill the screen boldly and immersively while ensuring
 * every section remains 100% self-contained with no overflow.
 */

import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import "./landing.css";

export default function LandingPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [activeSection, setActiveSection] = useState<string>("hero");

  // Scroll to section helper
  const scrollTo = (id: string) => {
    const el = document.getElementById(id);
    if (el) {
      el.scrollIntoView({ behavior: "smooth" });
    }
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const gl = canvas.getContext("webgl", { antialias: true, alpha: true });
    if (!gl) {
      console.error("WebGL not supported");
      return;
    }

    const vertexShaderSource = `
      attribute vec2 aPosition;
      void main() {
        gl_Position = vec4(aPosition, 0.0, 1.0);
      }
    `;

    const fragmentShaderSource = `
      precision mediump float;
      uniform vec2 iResolution;
      uniform float iTime;
      uniform float uHue;
      uniform float uIntensity;
      uniform float uSize;
      uniform float uSpeed;
      uniform float uScroll;
      
      #define OCTAVE_COUNT 8

      vec3 hsv2rgb(vec3 c) {
          vec3 rgb = clamp(abs(mod(c.x * 6.0 + vec3(0.0,4.0,2.0), 6.0) - 3.0) - 1.0, 0.0, 1.0);
          return c.z * mix(vec3(1.0), rgb, c.y);
      }

      float hash12(vec2 p) {
          vec3 p3 = fract(vec3(p.xyx) * .1031);
          p3 += dot(p3, p3.yzx + 33.33);
          return fract((p3.x + p3.y) * p3.z);
      }

      mat2 rotate2d(float theta) {
          float c = cos(theta);
          float s = sin(theta);
          return mat2(c, -s, s, c);
      }

      float noise(vec2 p) {
          vec2 ip = floor(p);
          vec2 fp = fract(p);
          float a = hash12(ip);
          float b = hash12(ip + vec2(1.0, 0.0));
          float c = hash12(ip + vec2(0.0, 1.0));
          float d = hash12(ip + vec2(1.0, 1.0));
          
          vec2 t = smoothstep(0.0, 1.0, fp);
          return mix(mix(a, b, t.x), mix(c, d, t.x), t.y);
      }

      float fbm(vec2 p) {
          float value = 0.0;
          float amplitude = 0.5;
          for (int i = 0; i < OCTAVE_COUNT; ++i) {
              value += amplitude * noise(p);
              p *= rotate2d(0.45);
              p *= 2.0;
              amplitude *= 0.5;
          }
          return value;
      }

      void main() {
          vec2 uv = gl_FragCoord.xy / iResolution.xy;
          uv = 2.0 * uv - 1.0;
          uv.x *= iResolution.x / iResolution.y;
          
          float time = iTime * (uSpeed + uScroll * 0.8);
          
          float path = uv.x + (fbm(uv * (uSize + uScroll * 0.5) + 0.8 * time) * 2.5 - 1.25);
          float dist = abs(path);
          
          float dynamicHue = uHue + uScroll * 60.0;
          vec3 baseColor = hsv2rgb(vec3(dynamicHue / 360.0, 0.85, 0.95));
          
          float intensity = uIntensity * (1.0 + uScroll * 1.5);
          float flicker = mix(0.0, 0.15, noise(vec2(time * 0.5, time)));
          
          vec3 col = baseColor * (flicker / max(dist, 0.004)) * intensity;
          col *= smoothstep(1.5, -0.5, abs(uv.y));
          
          gl_FragColor = vec4(col, 1.0);
      }
    `;

    const compileShader = (source: string, type: number): WebGLShader | null => {
      const shader = gl.createShader(type);
      if (!shader) return null;
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        console.error("Shader compile error:", gl.getShaderInfoLog(shader));
        gl.deleteShader(shader);
        return null;
      }
      return shader;
    };

    const vertexShader = compileShader(vertexShaderSource, gl.VERTEX_SHADER);
    const fragmentShader = compileShader(fragmentShaderSource, gl.FRAGMENT_SHADER);
    if (!vertexShader || !fragmentShader) return;

    const program = gl.createProgram();
    if (!program) return;
    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);

    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error("Program link error:", gl.getProgramInfoLog(program));
      return;
    }
    gl.useProgram(program);

    const vertices = new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

    const aPosition = gl.getAttribLocation(program, "aPosition");
    gl.enableVertexAttribArray(aPosition);
    gl.vertexAttribPointer(aPosition, 2, gl.FLOAT, false, 0, 0);

    const uniforms = {
      iResolution: gl.getUniformLocation(program, "iResolution"),
      iTime: gl.getUniformLocation(program, "iTime"),
      uHue: gl.getUniformLocation(program, "uHue"),
      uIntensity: gl.getUniformLocation(program, "uIntensity"),
      uSize: gl.getUniformLocation(program, "uSize"),
      uSpeed: gl.getUniformLocation(program, "uSpeed"),
      uScroll: gl.getUniformLocation(program, "uScroll"),
    };

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      gl.viewport(0, 0, canvas.width, canvas.height);
    };
    window.addEventListener("resize", resize);
    resize();

    let scrollYVal = 0;
    const scrollContainer = scrollContainerRef.current;

    const updateScrollProgress = () => {
      if (!scrollContainer) return;
      const sections = scrollContainer.querySelectorAll("section");

      sections.forEach((section) => {
        const rect = section.getBoundingClientRect();
        if (rect.top < window.innerHeight * 0.5 && rect.bottom > window.innerHeight * 0.5) {
          section.classList.add("section-visible");
          if (section.id) {
            setActiveSection(section.id);
          }
        } else {
          section.classList.remove("section-visible");
        }
      });
    };

    const handleScroll = () => {
      if (!scrollContainer) return;
      const maxScroll = scrollContainer.scrollHeight - scrollContainer.clientHeight;
      scrollYVal = maxScroll > 0 ? scrollContainer.scrollTop / maxScroll : 0;
      updateScrollProgress();
    };

    if (scrollContainer) {
      scrollContainer.addEventListener("scroll", handleScroll, { passive: true });
    }

    const start = performance.now();
    let animationFrameId: number;

    const render = () => {
      const time = (performance.now() - start) / 1000;
      gl.uniform2f(uniforms.iResolution, canvas.width, canvas.height);
      gl.uniform1f(uniforms.iTime, time);
      gl.uniform1f(uniforms.uHue, 210);
      gl.uniform1f(uniforms.uIntensity, 1.2);
      gl.uniform1f(uniforms.uSize, 0.9);
      gl.uniform1f(uniforms.uSpeed, 0.8);
      gl.uniform1f(uniforms.uScroll, scrollYVal);

      gl.drawArrays(gl.TRIANGLES, 0, 6);
      animationFrameId = requestAnimationFrame(render);
    };
    render();

    updateScrollProgress();

    return () => {
      window.removeEventListener("resize", resize);
      if (scrollContainer) {
        scrollContainer.removeEventListener("scroll", handleScroll);
      }
      cancelAnimationFrame(animationFrameId);
      gl.deleteBuffer(buffer);
      gl.deleteProgram(program);
      gl.deleteShader(vertexShader);
      gl.deleteShader(fragmentShader);
    };
  }, []);

  return (
    <div className="landing-page-root">
      <canvas ref={canvasRef} id="lightning-bg" className="lightning-canvas" />

      {/* Frame Overlay */}
      <div className="fixed inset-0 pointer-events-none z-50 p-4 sm:p-6">
        <div className="w-full h-full border border-white/5 rounded-[2.5rem]" />
      </div>

      {/* Navigation Bar */}
      <nav className="fixed top-0 left-0 w-full z-50 flex items-center justify-between px-8 py-4 lg:px-16 bg-[#0a0a0b]/90 backdrop-blur-md border-b border-white/5 shadow-2xl">
        <div className="flex items-center space-x-3.5">
          <div className="w-10 h-10 bg-[#38bdf8] flex items-center justify-center rounded-xl shadow-[0_0_20px_rgba(56,189,248,0.45)] transition-transform hover:scale-110">
            <iconify-icon icon="lucide:anchor" class="text-[#0a0a0b] text-2xl" />
          </div>
          <div>
            <span className="text-2xl font-bold tracking-tighter uppercase leading-none block">Foreshore</span>
            <span className="text-[8px] font-bold tracking-[0.45em] text-[#38bdf8] uppercase">Marine Intelligence</span>
          </div>
        </div>
        <div className="hidden xl:flex items-center space-x-2 text-[10px] font-bold tracking-[0.2em] text-white/45 uppercase">
          <button onClick={() => scrollTo("hero")} id="nav-about" className="nav-link hover:text-white bg-transparent border-none cursor-pointer">
            About
          </button>
          <button onClick={() => scrollTo("problem")} id="nav-problem" className="nav-link hover:text-white bg-transparent border-none cursor-pointer">
            The Problem
          </button>
          <button onClick={() => scrollTo("solution")} id="nav-solution" className="nav-link hover:text-white bg-transparent border-none cursor-pointer">
            Our Solution
          </button>
          <button onClick={() => scrollTo("how-it-works")} id="nav-how-it-works" className="nav-link hover:text-white bg-transparent border-none cursor-pointer">
            How It Works
          </button>
          <button onClick={() => scrollTo("innovation")} id="nav-innovation" className="nav-link hover:text-white bg-transparent border-none cursor-pointer">
            The Innovation
          </button>
          <button onClick={() => scrollTo("surfaces")} id="nav-surfaces" className="nav-link hover:text-white bg-transparent border-none cursor-pointer">
            Two Surfaces
          </button>
        </div>
        <div className="flex items-center space-x-5">
          <Link
            to="/boat"
            id="cta-boat-nav"
            className="text-[10px] font-bold tracking-widest text-white/65 hover:text-white transition-all uppercase no-underline"
          >
            Boat UI
          </Link>
          <Link
            to="/console"
            id="cta-console-nav"
            className="px-6 py-2.5 bg-[#38bdf8] text-[#0a0a0b] rounded-full text-[10px] font-bold tracking-widest hover:scale-105 transition-all shadow-lg hover:shadow-[#38bdf8]/25 uppercase no-underline"
          >
            Shore Console
          </Link>
        </div>
      </nav>

      {/* Scroll Indicator Dots */}
      <div className="scroll-indicator">
        {["hero", "problem", "solution", "how-it-works", "innovation", "surfaces", "provenance"].map((sec) => (
          <div
            key={sec}
            className={`scroll-dot ${activeSection === sec ? "active" : ""}`}
            data-target={sec}
            onClick={() => scrollTo(sec)}
          />
        ))}
      </div>

      {/* Scroll Container */}
      <div ref={scrollContainerRef} className="scroll-container relative z-10" id="main-scroll">
        {/* Hero Section */}
        <section id="hero" className="landing-section section-visible">
          <div className="text-center px-6 reveal-mask max-w-7xl">
            <div className="inline-flex items-center space-x-2.5 px-6 py-2 rounded-full bg-[#38bdf8]/10 border border-[#38bdf8]/20 text-[#38bdf8] text-[11px] font-bold uppercase tracking-[0.24em] mb-7 shadow-xl">
              <iconify-icon icon="lucide:shield-check" class="text-base" />
              <span>SIH 2026 · Problem SIH26176 · ISRO / Dept of Space</span>
            </div>
            <h1 className="text-5xl sm:text-6xl md:text-7xl lg:text-8xl font-bold tracking-tighter mb-6 leading-[0.88] accent-glow">
              MARINE<br />FORESIGHT
            </h1>
            <div className="space-y-4 mb-10">
              <p className="text-2xl md:text-4xl text-white font-light tracking-tight">
                Reasoning ashore. <span className="text-[#38bdf8] font-bold">Decision aboard.</span>
              </p>
              <p className="text-base md:text-xl text-white/45 max-w-3xl mx-auto font-light leading-relaxed italic border-l border-white/10 pl-5 py-0.5">
                &ldquo;SAMUDRA tells you what the advisory says. FORESHORE tells you what it means for your boat, tonight, and why.&rdquo;
              </p>
            </div>
            <div className="flex flex-col sm:flex-row items-center justify-center space-y-4 sm:space-y-0 sm:space-x-8">
              <button
                id="hero-cta-reply"
                onClick={() => scrollTo("problem")}
                className="px-16 py-5 bg-white text-[#0a0a0b] rounded-full font-bold text-lg shadow-[0_0_45px_rgba(255,255,255,0.3)] hover:scale-105 active:scale-95 transition-all cursor-pointer border-none"
              >
                REPLY
              </button>
              <Link
                to="/console"
                id="hero-cta-console"
                className="px-16 py-5 border border-white/20 rounded-full font-bold text-lg hover:bg-white/5 hover:border-white/40 transition-all text-white no-underline inline-block"
              >
                SHORE CONSOLE
              </Link>
            </div>
          </div>
        </section>

        {/* Section 1: The Problem */}
        <section id="problem" className="landing-section">
          <div className="max-w-7xl w-full reveal-mask px-4">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-10 items-end mb-8">
              <div>
                <span className="text-[#38bdf8] text-[11px] font-bold tracking-[0.35em] uppercase">01 / The Problem</span>
                <h2 className="text-4xl md:text-6xl lg:text-7xl font-bold tracking-tighter accent-glow mt-2 mb-2 leading-[0.92]">
                  The Communication Gap That Costs Lives
                </h2>
              </div>
              <div className="space-y-2 pb-1">
                <p className="text-base md:text-xl text-white/80 leading-relaxed font-light">
                  Fishing boats operate 100–150 km offshore. Mobile and VHF radio signals die at 10–20 km from the coastline.
                </p>
                <p className="text-xs md:text-base text-white/50 leading-relaxed">
                  When Cyclone Ockhi struck in 2017, the IMD issued warnings with 48 hours of lead time—but they never reached the small boats already at sea. In 2024 alone, 529+ fishermen were arrested for drifting across the IMBL due to lack of geofence awareness.
                </p>
              </div>
            </div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-5">
              <div className="glass-card p-7 rounded-[2rem] border-l-4 border-l-red-500/60 hover-glow transition-all">
                <div className="text-5xl md:text-6xl font-bold mb-1 tracking-tighter">
                  10–20<span className="text-2xl text-white/40 ml-1">km</span>
                </div>
                <div className="text-xs text-white/55 font-bold tracking-wider uppercase">Mobile & VHF cutoff</div>
                <p className="mt-2 text-[11px] text-white/30 leading-snug uppercase">The offshore cliff where safety messages stop.</p>
              </div>
              <div className="glass-card p-7 rounded-[2rem] border-l-4 border-l-white/20 hover-glow transition-all">
                <div className="text-5xl md:text-6xl font-bold mb-1 tracking-tighter">
                  100–150<span className="text-2xl text-white/40 ml-1">km</span>
                </div>
                <div className="text-xs text-white/55 font-bold tracking-wider uppercase">Active fishing distance</div>
                <p className="mt-2 text-[11px] text-white/30 leading-snug uppercase">Standard operational radius for small boats.</p>
              </div>
              <div className="glass-card p-7 rounded-[2rem] border-l-4 border-l-red-500/60 hover-glow transition-all">
                <div className="text-5xl md:text-6xl font-bold mb-1 tracking-tighter">529+</div>
                <div className="text-xs text-white/55 font-bold tracking-wider uppercase">Arrests in 2024</div>
                <p className="mt-2 text-[11px] text-white/30 leading-snug uppercase">Accidental IMBL drifts in Palk Bay & Mannar.</p>
              </div>
              <div className="glass-card p-7 rounded-[2rem] border-l-4 border-l-[#38bdf8]/60 hover-glow transition-all">
                <div className="text-5xl md:text-6xl font-bold mb-1 tracking-tighter">
                  48<span className="text-2xl text-white/40 ml-1">hrs</span>
                </div>
                <div className="text-xs text-white/55 font-bold tracking-wider uppercase">Unreachable lead time</div>
                <p className="mt-2 text-[11px] text-white/30 leading-snug uppercase">Critical safety window during Ockhi lost at sea.</p>
              </div>
            </div>
          </div>
        </section>

        {/* Section 2: Our Solution */}
        <section id="solution" className="landing-section">
          <div className="max-w-7xl w-full reveal-mask px-4">
            <div className="text-center mb-7">
              <span className="text-[#38bdf8] text-[11px] font-bold tracking-[0.35em] uppercase">02 / Our Solution</span>
              <h2 className="text-4xl md:text-6xl lg:text-7xl font-bold tracking-tighter accent-glow mt-1.5 mb-2.5 max-w-5xl mx-auto leading-[0.92]">
                Every System Tells You What the Sea Is Doing.<br />
                <span className="text-[#38bdf8]">We Tell You What You Should Do.</span>
              </h2>
              <p className="text-base md:text-lg text-white/60 leading-relaxed max-w-4xl mx-auto font-light">
                FORESHORE is an agentic marine intelligence platform. Specialized AI agents collaborate to answer any marine safety question with a single, unambiguous verdict.
              </p>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <div className="glass-card p-8 md:p-9 rounded-[2.5rem] border-t-6 border-t-[#10b981] group hover-glow transition-all">
                <div className="flex justify-between items-start mb-4">
                  <div className="w-14 h-14 rounded-2xl bg-[#10b981]/10 flex items-center justify-center text-[#10b981] shadow-lg shadow-[#10b981]/15">
                    <iconify-icon icon="lucide:check-circle" class="text-3xl" />
                  </div>
                  <div className="text-3xl md:text-4xl font-black text-[#10b981] tracking-tighter">GO</div>
                </div>
                <h3 className="text-xl md:text-2xl font-bold mb-2 tracking-tight">Favorable Conditions</h3>
                <p className="text-white/55 text-sm leading-relaxed italic">
                  &ldquo;Conditions are favorable across sea-state, wind, and swell. Safe to proceed with standard precautions.&rdquo;
                </p>
              </div>
              <div className="glass-card p-8 md:p-9 rounded-[2.5rem] border-t-6 border-t-[#f59e0b] group hover-glow transition-all">
                <div className="flex justify-between items-start mb-4">
                  <div className="w-14 h-14 rounded-2xl bg-[#f59e0b]/10 flex items-center justify-center text-[#f59e0b] shadow-lg shadow-[#f59e0b]/15">
                    <iconify-icon icon="lucide:alert-circle" class="text-3xl" />
                  </div>
                  <div className="text-2xl md:text-3xl font-black text-[#f59e0b] tracking-tighter leading-none text-right">
                    GO WITH<br />CAUTION
                  </div>
                </div>
                <h3 className="text-xl md:text-2xl font-bold mb-2 tracking-tight">Marginal Outlook</h3>
                <p className="text-white/55 text-sm leading-relaxed italic">
                  &ldquo;Localized gusting forecast. Proceed with heightened awareness, shorter trip envelopes, and active monitoring.&rdquo;
                </p>
              </div>
              <div className="glass-card p-8 md:p-9 rounded-[2.5rem] border-t-6 border-t-[#ef4444] group hover-glow transition-all">
                <div className="flex justify-between items-start mb-4">
                  <div className="w-14 h-14 rounded-2xl bg-[#ef4444]/10 flex items-center justify-center text-[#ef4444] shadow-lg shadow-[#ef4444]/15">
                    <iconify-icon icon="lucide:x-circle" class="text-3xl" />
                  </div>
                  <div className="text-2xl md:text-3xl font-black text-[#ef4444] tracking-tighter leading-none text-right">
                    DO NOT<br />ADVISE
                  </div>
                </div>
                <h3 className="text-xl md:text-2xl font-bold mb-2 tracking-tight">Extreme Risk</h3>
                <p className="text-white/55 text-sm leading-relaxed italic">
                  &ldquo;Thresholds exceeded. Automatically routing to Coast Guard emergency line (1554). Safety invariant triggered.&rdquo;
                </p>
              </div>
            </div>
            <div className="mt-6 text-center">
              <div className="inline-block px-7 py-2.5 bg-white/5 border border-white/10 rounded-xl">
                <span className="text-white/45 text-xs font-medium">Safety Policy: </span>
                <span className="text-[#38bdf8] text-xs font-bold">Foreshore may be more cautious than official bulletins, but never more permissive.</span>
              </div>
            </div>
          </div>
        </section>

        {/* Section 3: How It Works */}
        <section id="how-it-works" className="landing-section">
          <div className="max-w-7xl w-full reveal-mask px-4">
            <div className="mb-5">
              <span className="text-[#38bdf8] text-[11px] font-bold tracking-[0.35em] uppercase">03 / The Pipeline</span>
              <h2 className="text-4xl md:text-6xl lg:text-7xl font-bold tracking-tighter accent-glow mt-1 leading-[0.92]">
                From Question to Verdict in Seconds
              </h2>
              <p className="text-sm md:text-base text-white/50 max-w-4xl mt-1.5 leading-relaxed font-light">
                A single query triggers an asynchronous agentic reasoning pipeline. Complex weather models are synthesized into spoken guidance.
              </p>
            </div>

            {/* Flow Diagram */}
            <div className="relative flex flex-wrap lg:flex-nowrap justify-between gap-2.5 mb-6">
              <div className="absolute top-1/2 left-0 w-full h-[1px] bg-white/5 -z-10 hidden lg:block" />
              {[
                { stage: "01", name: "Query", icon: "lucide:message-square" },
                { stage: "02", name: "Normalisation", icon: "lucide:languages" },
                { stage: "03", name: "Planning", icon: "lucide:git-pull-request" },
                { stage: "04", name: "Retrieval", icon: "lucide:download-cloud" },
                { stage: "05", name: "Verdict Engine", icon: "lucide:settings" },
                { stage: "06", name: "Ceiling Check", icon: "lucide:lock" },
                { stage: "07", name: "Synthesis", icon: "lucide:waves" },
                { stage: "08", name: "Handoff", icon: "lucide:satellite-receiver" },
              ].map((step) => (
                <div key={step.stage} className="flex-1 min-w-[95px] glass-card p-3 rounded-xl text-center space-y-1 hover:border-[#38bdf8]/40 transition-all">
                  <iconify-icon icon={step.icon} class="text-xl text-[#38bdf8]" />
                  <div className="text-[8px] font-bold tracking-widest text-white/40 uppercase">Stage {step.stage}</div>
                  <div className="text-xs font-bold truncate">{step.name}</div>
                </div>
              ))}
            </div>

            {/* Specialist Grid */}
            <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
              <div className="glass-card p-5 rounded-2xl bg-gradient-to-br from-[#38bdf8]/5 to-transparent">
                <iconify-icon icon="lucide:thermometer-sun" class="text-3xl text-[#38bdf8] mb-2.5" />
                <h4 className="text-sm font-bold mb-1 tracking-tight">WeatherIntel</h4>
                <p className="text-[11px] text-white/40 leading-snug">
                  IMD ACWC Chennai coastal bulletin, Open-Meteo marine forecasts, 1-hour resolution data retrieval.
                </p>
              </div>
              <div className="glass-card p-5 rounded-2xl bg-gradient-to-br from-[#38bdf8]/5 to-transparent">
                <iconify-icon icon="lucide:line-chart" class="text-3xl text-[#38bdf8] mb-2.5" />
                <h4 className="text-sm font-bold mb-1 tracking-tight">OceanAnalytics</h4>
                <p className="text-[11px] text-white/40 leading-snug">
                  INCOIS MWW3 wave model with data assimilation (11 km nest) for real-time swell profiles.
                </p>
              </div>
              <div className="glass-card p-5 rounded-2xl bg-gradient-to-br from-[#38bdf8]/5 to-transparent">
                <iconify-icon icon="lucide:map-pin" class="text-3xl text-[#38bdf8] mb-2.5" />
                <h4 className="text-sm font-bold mb-1 tracking-tight">Geospatial</h4>
                <p className="text-[11px] text-white/40 leading-snug">
                  1974/1976 IMBL treaties, marine national parks, and coral reef geofence boundary awareness.
                </p>
              </div>
              <div className="glass-card p-5 rounded-2xl bg-gradient-to-br from-[#38bdf8]/5 to-transparent">
                <iconify-icon icon="lucide:shield-alert" class="text-3xl text-[#38bdf8] mb-2.5" />
                <h4 className="text-sm font-bold mb-1 tracking-tight">RiskAssessment</h4>
                <p className="text-[11px] text-white/40 leading-snug">
                  Capsize-risk curves calibrated to vessel classes (motorised, vallam, trawler) based on Hs.
                </p>
              </div>
              <div className="glass-card p-5 rounded-2xl bg-gradient-to-br from-[#38bdf8]/5 to-transparent">
                <iconify-icon icon="lucide:compass" class="text-3xl text-[#38bdf8] mb-2.5" />
                <h4 className="text-sm font-bold mb-1 tracking-tight">Router</h4>
                <p className="text-[11px] text-white/40 leading-snug">
                  True A* cost-field routing (wind, currents, depth penalty) — safety-first navigation logic.
                </p>
              </div>
            </div>
          </div>
        </section>

        {/* Section 4: The Innovation */}
        <section id="innovation" className="landing-section">
          <div className="max-w-7xl w-full reveal-mask px-4">
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-10 items-center">
              <div className="lg:col-span-6 space-y-4">
                <span className="text-[#38bdf8] text-[11px] font-bold tracking-[0.35em] uppercase">04 / The Innovation</span>
                <h2 className="text-4xl md:text-6xl lg:text-7xl font-bold tracking-tighter accent-glow leading-[0.92] mt-1.5 mb-3">
                  220 Bits That Cross the Satellite
                </h2>
                <p className="text-base text-white/50 leading-relaxed font-light">
                  Multi-gigabyte models cannot run on small boats. We do the heavy reasoning ashore and broadcast only the critical decision payload.
                </p>
                <div className="p-5 glass-card rounded-2xl border-l-4 border-l-[#38bdf8]">
                  <h4 className="text-[#38bdf8] text-sm font-bold mb-1">NavIC Protocol Integration</h4>
                  <p className="text-xs text-white/45 leading-relaxed">
                    Our verdict fits ISRO&apos;s existing IRNSS broadcast sub-frame unchanged. Same ICD, same bit budget, same 12-second cadence.
                  </p>
                </div>
              </div>

              <div className="lg:col-span-6 space-y-5">
                <div className="text-center">
                  <div className="text-[9.5px] font-bold tracking-[0.35em] text-white/35 uppercase mb-2.5">
                    Sub-Frame Bit Allocation (286 Bits)
                  </div>
                  <div className="bit-bar">
                    <div className="bit-segment w-[3%] border-none" title="Telemetry">TLM</div>
                    <div className="bit-segment w-[6%]">TOWC</div>
                    <div className="bit-segment w-[2%]">RSV</div>
                    <div className="bit-segment w-[2%]">ID</div>
                    <div className="bit-segment flex-[220] bit-active">DECISION PAYLOAD (220 BITS)</div>
                    <div className="bit-segment w-[2%]">RSV</div>
                    <div className="bit-segment w-[8%]">CRC</div>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="glass-card p-5 rounded-2xl">
                    <div className="text-[#38bdf8] text-3xl font-bold">220 BITS</div>
                    <div className="text-[10px] text-white/45 font-bold tracking-widest uppercase mt-0.5">Payload Size</div>
                  </div>
                  <div className="glass-card p-5 rounded-2xl">
                    <div className="text-[#38bdf8] text-3xl font-bold">12 SEC</div>
                    <div className="text-[10px] text-white/45 font-bold tracking-widest uppercase mt-0.5">Broadcast Cadence</div>
                  </div>
                  <div className="glass-card p-5 rounded-2xl col-span-2">
                    <div className="text-white/65 text-xs font-bold flex items-center mb-1">
                      <iconify-icon icon="lucide:binary" class="mr-2 text-[#38bdf8] text-sm" />
                      PAYLOAD DECOMPOSITION
                    </div>
                    <p className="text-[10.5px] text-white/35 leading-relaxed">
                      Verdict (2 bits) + Binding Constraint (14 bits) + Swell Margin (14 bits) + Validity Envelope (10 bits) + Handoff (180 bits).
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* Section 5: Two Surfaces */}
        <section id="surfaces" className="landing-section">
          <div className="max-w-6xl w-full reveal-mask px-4">
            <div className="text-center mb-7">
              <span className="text-[#38bdf8] text-[11px] font-bold tracking-[0.35em] uppercase">05 / Ecosystem</span>
              <h2 className="text-4xl md:text-6xl lg:text-7xl font-bold tracking-tighter accent-glow mt-1.5 leading-[0.92]">
                Built for Those Who Need It Most
              </h2>
              <p className="text-base text-white/60 mt-1.5 max-w-3xl mx-auto font-light">
                One agentic core powering two specialized, life-saving interfaces.
              </p>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
              <div className="glass-card p-9 md:p-10 rounded-[2.5rem] space-y-4 hover-glow transition-all border-t-6 border-t-[#38bdf8] relative overflow-hidden">
                <div className="flex items-center justify-between">
                  <iconify-icon icon="lucide:smartphone" class="text-5xl text-[#38bdf8]" />
                  <div className="inline-block px-4 py-1.5 rounded-full bg-[#38bdf8]/10 text-[#38bdf8] text-[10px] font-bold tracking-[0.18em] uppercase">
                    Tamil-first · Voice-first
                  </div>
                </div>
                <div>
                  <h3 className="text-3xl font-bold mb-2 tracking-tight">Boat UI</h3>
                  <p className="text-white/50 text-sm leading-relaxed mb-6">
                    Designed for high-stress marine environments. Extra-large touch targets, voice synthesis, and satellite sub-frame parsing.
                  </p>
                  <Link
                    to="/boat"
                    id="cta-boat-link"
                    className="inline-flex items-center px-8 py-3.5 bg-white text-[#0a0a0b] rounded-full text-sm font-bold hover:scale-105 active:scale-95 transition-all shadow-lg no-underline"
                  >
                    Open Boat UI
                    <iconify-icon icon="lucide:arrow-right" class="ml-2.5" />
                  </Link>
                </div>
              </div>
              <div className="glass-card p-9 md:p-10 rounded-[2.5rem] space-y-4 hover-glow transition-all border-t-6 border-t-white/20 relative overflow-hidden">
                <div className="flex items-center justify-between">
                  <iconify-icon icon="lucide:monitor" class="text-5xl text-white/35" />
                  <div className="inline-block px-4 py-1.5 rounded-full bg-white/5 text-white/45 text-[10px] font-bold tracking-[0.18em] uppercase">
                    Fleet View · Disaster Management
                  </div>
                </div>
                <div>
                  <h3 className="text-3xl font-bold mb-2 tracking-tight">Shore Console</h3>
                  <p className="text-white/50 text-sm leading-relaxed mb-6">
                    Built for fisheries officers and Coast Guard operators. Real-time fleet radar, cyclone uncertainty cones, and agent audit traces.
                  </p>
                  <Link
                    to="/console"
                    id="cta-console-link"
                    className="inline-flex items-center px-8 py-3.5 border border-white/20 rounded-full text-sm font-bold hover:bg-white/5 transition-all text-white no-underline"
                  >
                    Open Shore Console
                    <iconify-icon icon="lucide:arrow-right" class="ml-2.5" />
                  </Link>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* Section 6: Provenance & Footer */}
        <section id="provenance" className="landing-section">
          <div className="max-w-7xl w-full reveal-mask px-4">
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-center mb-6">
              <div className="lg:col-span-4">
                <span className="text-[#38bdf8] text-[11px] font-bold tracking-[0.35em] uppercase">06 / Data Provenance</span>
                <h2 className="text-3xl md:text-5xl font-bold accent-glow mt-1 leading-[0.95]">Live Scientific Endpoints</h2>
                <p className="text-sm text-white/45 mt-2 leading-relaxed font-light">
                  We don&apos;t hallucinate metrics. Our agents call official, keyless API endpoints maintained by government and inter-governmental agencies.
                </p>
              </div>
              <div className="lg:col-span-8 grid grid-cols-2 sm:grid-cols-3 gap-3 text-[10.5px]">
                <div className="p-4 glass-card rounded-2xl hover:bg-white/5 transition-all group">
                  <div className="flex justify-between items-center mb-1">
                    <span className="font-bold text-[#38bdf8] text-sm">IMD ACWC</span>
                    <span className="px-2 py-[2px] rounded bg-[#38bdf8]/10 text-[#38bdf8] text-[8px]">WEATHER</span>
                  </div>
                  <div className="text-white/35 text-[9.5px] uppercase">12-hr Coastal Bulletins</div>
                </div>
                <div className="p-4 glass-card rounded-2xl hover:bg-white/5 transition-all group">
                  <div className="flex justify-between items-center mb-1">
                    <span className="font-bold text-[#38bdf8] text-sm">INCOIS OSF</span>
                    <span className="px-2 py-[2px] rounded bg-[#38bdf8]/10 text-[#38bdf8] text-[8px]">OCEAN</span>
                  </div>
                  <div className="text-white/35 text-[9.5px] uppercase">MWW3 wave model nest</div>
                </div>
                <div className="p-4 glass-card rounded-2xl hover:bg-white/5 transition-all group">
                  <div className="flex justify-between items-center mb-1">
                    <span className="font-bold text-[#38bdf8] text-sm">Marine Regions</span>
                    <span className="px-2 py-[2px] rounded bg-[#38bdf8]/10 text-[#38bdf8] text-[8px]">GEOFENCE</span>
                  </div>
                  <div className="text-white/35 text-[9.5px] uppercase">1974/76 IMBL treaties</div>
                </div>
                <div className="p-4 glass-card rounded-2xl hover:bg-white/5 transition-all group">
                  <div className="flex justify-between items-center mb-1">
                    <span className="font-bold text-[#38bdf8] text-sm">GDACS (JRC)</span>
                    <span className="px-2 py-[2px] rounded bg-[#38bdf8]/10 text-[#38bdf8] text-[8px]">DISASTER</span>
                  </div>
                  <div className="text-white/35 text-[9.5px] uppercase">Cyclone track cones</div>
                </div>
                <div className="p-4 glass-card rounded-2xl hover:bg-white/5 transition-all group">
                  <div className="flex justify-between items-center mb-1">
                    <span className="font-bold text-[#38bdf8] text-sm">GEBCO</span>
                    <span className="px-2 py-[2px] rounded bg-[#38bdf8]/10 text-[#38bdf8] text-[8px]">DEPTH</span>
                  </div>
                  <div className="text-white/35 text-[9.5px] uppercase">Bathymetric shoal data</div>
                </div>
                <div className="p-4 glass-card rounded-2xl hover:bg-white/5 transition-all group">
                  <div className="flex justify-between items-center mb-1">
                    <span className="font-bold text-[#38bdf8] text-sm">ISRO Bhuvan</span>
                    <span className="px-2 py-[2px] rounded bg-[#38bdf8]/10 text-[#38bdf8] text-[8px]">SATELLITE</span>
                  </div>
                  <div className="text-white/35 text-[9.5px] uppercase">OCM ocean colour maps</div>
                </div>
              </div>
            </div>

            {/* Footer row */}
            <footer className="pt-4 border-t border-white/10">
              <div className="flex flex-col md:flex-row justify-between items-center text-center md:text-left gap-4">
                <div className="space-y-1">
                  <div className="flex items-center justify-center md:justify-start space-x-2.5">
                    <div className="w-6 h-6 bg-[#38bdf8] flex items-center justify-center rounded-lg">
                      <iconify-icon icon="lucide:anchor" class="text-[#0a0a0b] text-sm" />
                    </div>
                    <span className="text-lg font-black uppercase tracking-widest">FORESHORE</span>
                  </div>
                  <div className="text-[10px] text-white/35 space-x-2 uppercase tracking-wider">
                    <span>SIH 2026 (SIH26176)</span>
                    <span>·</span>
                    <span>ISRO / Dept of Space</span>
                    <span>·</span>
                    <span>Disaster Management</span>
                  </div>
                </div>

                <div className="flex items-center space-x-6 text-[10px] font-bold tracking-wider text-white/45 uppercase">
                  <button onClick={() => scrollTo("hero")} id="footer-about" className="hover:text-[#38bdf8] bg-transparent border-none p-0 cursor-pointer">
                    About
                  </button>
                  <Link to="/boat" id="footer-boat" className="hover:text-[#38bdf8] no-underline">
                    Boat UI
                  </Link>
                  <Link to="/console" id="footer-console" className="hover:text-[#38bdf8] no-underline">
                    Shore Console
                  </Link>
                </div>

                <div className="text-[10px] text-[#38bdf8] font-bold tracking-wider uppercase italic">
                  ORCA: Marine EcOsystem Reasoning with Collaborative Agents
                </div>
              </div>
            </footer>
          </div>
        </section>
      </div>
    </div>
  );
}
