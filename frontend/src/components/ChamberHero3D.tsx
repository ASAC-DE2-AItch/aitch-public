// S1 히어로 — Three.js 식각 챔버 (쿼터 컷어웨이 · PBR 금속 · 플라즈마 발광)
// 자이스/AURA 격차의 본체: "사진급 렌더된 물체". SVG 단면(FocusChamber 구판) 대체.
// 테마 연동: CSS 변수에서 액센트/경보색 읽고 data-theme 변화 감지.
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#5e9be6";
}

export interface HotspotDef {
  num: number;
  anchor: [number, number, number]; // 월드 좌표
  alarm: boolean;
  label: string;   // 미니 카드 라벨 (예: "DC bias C11")
  value: string;   // 큰 값
  unit: string;
}

export default function ChamberHero3D({ abnormal, hotspots, height = 460 }: {
  abnormal: boolean;
  hotspots: HotspotDef[];
  height?: number;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const badgeRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;

    /* ---- 기본 셋업 ---- */
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.localClippingEnabled = true;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.98;
    wrap.appendChild(renderer.domElement);
    renderer.domElement.style.position = "absolute";
    renderer.domElement.style.inset = "0";
    renderer.domElement.style.zIndex = "1";

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(27, 1, 0.1, 60);

    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

    /* ---- 컷어웨이 클리핑 (앞쪽 1/4 절개 = 단면 뷰) ---- */
    const clip = new THREE.Plane(new THREE.Vector3(-0.62, 0, -0.79).normalize(), 0.42);
    const clipped = { clippingPlanes: [clip], clipShadows: true } as const;

    /* ---- 재질 ---- */
    const steel = new THREE.MeshStandardMaterial({
      color: 0xcdd6e2, metalness: 0.72, roughness: 0.28, side: THREE.DoubleSide, ...clipped,
    });
    const steelDark = new THREE.MeshStandardMaterial({
      color: 0x9aa7b8, metalness: 0.7, roughness: 0.38, side: THREE.DoubleSide, ...clipped,
    });
    const liner = new THREE.MeshStandardMaterial({
      color: 0x6b7789, metalness: 0.35, roughness: 0.62, side: THREE.BackSide, ...clipped,
    });
    const waferMat = new THREE.MeshStandardMaterial({
      color: 0xdfe6ef, metalness: 0.6, roughness: 0.16, ...clipped,
    });

    /* ---- 지오메트리 ---- */
    const g = new THREE.Group();
    scene.add(g);

    // 본체 + 내벽
    g.add(new THREE.Mesh(new THREE.CylinderGeometry(1.5, 1.5, 2.3, 96, 1, true), steel));
    g.add(new THREE.Mesh(new THREE.CylinderGeometry(1.42, 1.42, 2.28, 96, 1, true), liner));
    // 상/하 리드
    const lidTop = new THREE.Mesh(new THREE.CylinderGeometry(1.58, 1.58, 0.16, 96), steel);
    lidTop.position.y = 1.23; g.add(lidTop);
    const lidBot = new THREE.Mesh(new THREE.CylinderGeometry(1.58, 1.58, 0.18, 96), steel);
    lidBot.position.y = -1.24; g.add(lidBot);
    // 플랜지 링 + 볼트
    const flange = new THREE.Mesh(new THREE.TorusGeometry(1.52, 0.055, 20, 96), steelDark);
    flange.rotation.x = Math.PI / 2; flange.position.y = 1.12; g.add(flange);
    const flangeB = flange.clone(); flangeB.position.y = -1.12; g.add(flangeB);
    const boltGeo = new THREE.CylinderGeometry(0.045, 0.045, 0.1, 12);
    for (let i = 0; i < 14; i++) {
      const a = (i / 14) * Math.PI * 2;
      const bolt = new THREE.Mesh(boltGeo, steelDark);
      bolt.position.set(Math.cos(a) * 1.52, 1.32, Math.sin(a) * 1.52);
      g.add(bolt);
    }
    // 가스 인렛 (상단 튜브 + 링)
    const inlet = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.16, 0.55, 32), steel);
    inlet.position.y = 1.55; g.add(inlet);
    const inletRing = new THREE.Mesh(new THREE.TorusGeometry(0.19, 0.03, 12, 32), steelDark);
    inletRing.rotation.x = Math.PI / 2; inletRing.position.y = 1.75; g.add(inletRing);
    // 샤워헤드
    const shower = new THREE.Mesh(new THREE.CylinderGeometry(1.02, 1.02, 0.1, 64), steelDark);
    shower.position.y = 0.66; g.add(shower);
    // 페데스탈(ESC) + 스템 + 웨이퍼
    const ped = new THREE.Mesh(new THREE.CylinderGeometry(0.6, 0.66, 0.3, 64), steelDark);
    ped.position.y = -0.5; g.add(ped);
    const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.16, 0.62, 32), steel);
    stem.position.y = -0.95; g.add(stem);
    const wafer = new THREE.Mesh(new THREE.CylinderGeometry(0.64, 0.64, 0.028, 64), waferMat);
    wafer.position.y = -0.33; g.add(wafer);
    // 배기 포트 (측면 튜브)
    const port = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.2, 0.7, 32), steel);
    port.rotation.z = Math.PI / 2; port.position.set(-1.75, -0.62, 0); g.add(port);

    /* ---- 플라즈마 (발광 2겹 + 광원) ---- */
    const accent = new THREE.Color(cssVar("--accent"));
    const warn = new THREE.Color(cssVar("--status-warning-fg"));
    const plasmaOuter = new THREE.Mesh(
      new THREE.SphereGeometry(0.82, 48, 32),
      new THREE.MeshBasicMaterial({
        color: accent, transparent: true, opacity: 0.14,
        blending: THREE.AdditiveBlending, depthWrite: false, ...clipped,
      }),
    );
    plasmaOuter.scale.set(1, 0.5, 1); plasmaOuter.position.y = 0.08; g.add(plasmaOuter);
    const plasmaCore = new THREE.Mesh(
      new THREE.SphereGeometry(0.42, 48, 32),
      new THREE.MeshBasicMaterial({
        color: accent, transparent: true, opacity: 0.34,
        blending: THREE.AdditiveBlending, depthWrite: false, ...clipped,
      }),
    );
    plasmaCore.scale.set(1, 0.5, 1); plasmaCore.position.y = 0.05; g.add(plasmaCore);
    const plasmaLight = new THREE.PointLight(accent, 26, 7, 1.8);
    plasmaLight.position.set(0, 0.06, 0); g.add(plasmaLight);
    // 경보 시 웨이퍼 부근 앰버 보조광
    const warnLight = new THREE.PointLight(warn, abnormal ? 9 : 0, 4, 2);
    warnLight.position.set(0.5, -0.28, 0.4); g.add(warnLight);

    /* ---- 라이팅 보조 ---- */
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(4, 6, 3); scene.add(key);
    scene.add(new THREE.HemisphereLight(0xdfe8f5, 0x2a3340, 0.5));

    /* ---- 테마 변화 → 플라즈마 색 갱신 ---- */
    const applyTheme = () => {
      const a = new THREE.Color(cssVar("--accent"));
      (plasmaOuter.material as any).color = a;
      (plasmaCore.material as any).color = a;
      plasmaLight.color = a;
      warnLight.color = new THREE.Color(cssVar("--status-warning-fg"));
    };
    const mo = new MutationObserver(applyTheme);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    /* ---- 리사이즈 ---- */
    const resize = () => {
      const w = wrap.clientWidth, h = wrap.clientHeight;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    resize();

    /* ---- 루프: 미세 오비트 + 플라즈마 맥동 + 핫스팟 투영 ---- */
    const baseAngle = 0.62;
    const v = new THREE.Vector3();
    let raf = 0;
    const tick = (tms: number) => {
      const t = tms / 1000;
      const ang = baseAngle + Math.sin(t * 0.22) * 0.16;
      const camR = 8.9, camY = 1.8;
      camera.position.set(Math.sin(ang) * camR, camY, Math.cos(ang) * camR);
      camera.lookAt(0, -0.04, 0);

      const pulse = 1 + Math.sin(t * 1.4) * 0.025;
      plasmaOuter.scale.set(pulse, 0.55 * pulse, pulse);
      plasmaLight.intensity = 26 + Math.sin(t * 1.6) * 3;

      // 핫스팟 배지 투영
      hotspots.forEach((h, i) => {
        const el = badgeRefs.current[i];
        if (!el) return;
        v.set(...h.anchor).project(camera);
        const x = (v.x * 0.5 + 0.5) * wrap.clientWidth;
        const y = (-v.y * 0.5 + 0.5) * wrap.clientHeight;
        el.style.transform = `translate(${x + 10}px, ${y - 14}px)`;
      });

      renderer.render(scene, camera);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect(); mo.disconnect();
      pmrem.dispose(); renderer.dispose();
      scene.traverse((o: any) => {
        const m = o;
        if (m.geometry) m.geometry.dispose();
        if (m.material) (Array.isArray(m.material) ? m.material : [m.material]).forEach((x: any) => x.dispose());
      });
      wrap.removeChild(renderer.domElement);
    };
    // hotspots 좌표는 정적(참조 동일성 무시), abnormal만 재구성 트리거
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [abnormal]);

  return (
    <div ref={wrapRef} className="relative w-full overflow-hidden" style={{ height }}>
      {/* 접지 그림자 (HTML — 테마 자동) */}
      <div className="pointer-events-none absolute left-1/2 top-[76%] h-[10%] w-[58%] -translate-x-1/2 rounded-[50%]"
        style={{ background: "radial-gradient(ellipse at center, rgb(20 40 80 / 0.14), transparent 70%)" }} />
      {/* 떠있는 미니 스탯 카드 (AURA 히어로 문법 — 3D 투영 추적) */}
      {hotspots.map((h, i) => (
        <div key={h.num}
          ref={(el) => { badgeRefs.current[i] = el; }}
          className="float-card pointer-events-none absolute left-0 top-0"
          style={{ willChange: "transform", zIndex: 2, minWidth: 92 }}>
          <div className="w-label" style={{ fontSize: 10.5, color: h.alarm ? "var(--warn)" : "var(--text-muted)" }}>
            {h.label}
          </div>
          <div className="big-stat" style={{ fontSize: 22, color: h.alarm ? "var(--warn)" : "var(--text)" }}>
            {h.value}
            <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-faint)", marginLeft: 3 }}>{h.unit}</span>
          </div>
        </div>
      ))}
    </div>
  );
}
