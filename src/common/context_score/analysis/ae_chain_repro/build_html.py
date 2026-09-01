"""build_html.py — viz_data.json → 단일 HTML 시각화 (외부 의존 없음)."""
import json
from pathlib import Path

OUT = Path("/sessions/sleepy-zealous-cerf/mnt/outputs")
D = json.loads((OUT / "viz_data.json").read_text())

HTML = r"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AE 스코어 체인 — 실데이터 추적</title>
<style>
:root{--bg:#fbfaf9;--card:#fff;--ink:#1a1a18;--mut:#6b6b66;--line:#e5e3df;
--raw:#8b5cf6;--sc:#2563eb;--dr:#dc2626;--ok:#059669;--warn:#d97706;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,'Segoe UI','Malgun Gothic',sans-serif;line-height:1.6}
.wrap{max-width:1120px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:19px;margin:44px 0 4px;letter-spacing:-.01em}
h2 .num{display:inline-block;width:26px;height:26px;line-height:26px;text-align:center;
background:var(--ink);color:#fff;border-radius:7px;font-size:13px;margin-right:9px;vertical-align:2px}
.sub{color:var(--mut);font-size:13.5px;margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:14px 0}
.badge{display:inline-block;background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0;
border-radius:999px;padding:3px 11px;font-size:12px;font-weight:600;margin-right:6px}
.badge.w{background:#fffbeb;color:#92400e;border-color:#fde68a}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{padding:6px 7px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{background:#f6f5f3;font-weight:600;font-size:11.5px;color:var(--mut);text-align:right;
position:sticky;top:0}
td:first-child,th:first-child{text-align:left}
tr.hl{background:#fef9c3}
tr.seg2 td:nth-child(2){color:var(--dr);font-weight:600}
.scroll{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.pipe{display:flex;gap:0;align-items:stretch;flex-wrap:wrap}
.stage{flex:1;min-width:150px;background:#fff;border:1px solid var(--line);border-radius:10px;
padding:13px 14px;position:relative}
.stage+.stage{margin-left:22px}
.stage+.stage:before{content:'▶';position:absolute;left:-18px;top:50%;transform:translateY(-50%);
color:#c9c6c0;font-size:12px}
.stage .lb{font-size:10.5px;color:var(--mut);letter-spacing:.04em;text-transform:uppercase}
.stage .vl{font-size:23px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.02em;margin:2px 0}
.stage .fm{font-size:11px;color:var(--mut);font-family:ui-monospace,monospace}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.btn{background:#fff;border:1px solid var(--line);border-radius:9px;padding:9px 13px;cursor:pointer;
font-size:12.5px;font-family:inherit;text-align:left;transition:.12s}
.btn:hover{border-color:#b8b5ae}
.btn.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.btn b{display:block;font-size:13px;margin-bottom:1px}
.btn span{font-size:11px;opacity:.72;display:block;max-width:210px;white-space:normal;line-height:1.35}
svg{display:block;width:100%;height:auto;overflow:visible}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px}
.kv div{background:#f6f5f3;border-radius:9px;padding:11px 13px}
.kv .k{font-size:11px;color:var(--mut)}
.kv .v{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.note{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:13px 15px;font-size:13px;margin:14px 0}
.note b{color:#92400e}
code{background:#f1efec;padding:1.5px 5px;border-radius:4px;font-size:12px;
font-family:ui-monospace,'SF Mono',monospace}
.lgd{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:8px 0 0}
.lgd i{display:inline-block;width:11px;height:3px;border-radius:2px;vertical-align:3px;margin-right:5px}
.small{font-size:12px;color:var(--mut)}
</style></head><body><div class="wrap">

<h1>AE 스코어 체인 — 실데이터로 따라가기</h1>
<p class="sub">
ae_v3 번들(z16_rev3_seg1) + 실 FDC 데이터 15,919 wafer · 원 파이프라인 수학 재현<br>
<span class="badge">VAL 해시 fa500f3d 일치</span>
<span class="badge">calib 앵커 재현 오차 0.006%</span>
<span class="badge">L5 오경보 1.53% 일치</span>
<span class="badge">VAL 평균 ae_score 0.045 일치</span>
</p>

<h2><span class="num">1</span>한눈에 — 값이 어떻게 변해가나</h2>
<p class="sub">아래 wafer를 눌러 실제 값이 각 단계에서 어떻게 바뀌는지 보세요.</p>
<div class="btns" id="btns"></div>
<div class="card"><div class="pipe" id="pipe"></div>
<div class="small" style="margin-top:14px" id="pipenote"></div></div>

<h2><span class="num">2</span>ae_raw — 원점수 (unbounded)</h2>
<p class="sub">재구성 잔차의 조건화 Mahalanobis². 위로 열려 있어 "얼마나 심한가"가 끝까지 구분됩니다.</p>
<div class="card">
<div class="kv">
<div><div class="k">정상(seg1) 중앙값</div><div class="v">33.0</div></div>
<div><div class="k">정상 μ_raw</div><div class="v" id="mu"></div></div>
<div><div class="k">정상 σ_raw</div><div class="v" id="sd"></div></div>
<div><div class="k">이상(seg2) 중앙값</div><div class="v" style="color:var(--dr)">226.7</div></div>
<div><div class="k">관측 최대</div><div class="v" style="color:var(--dr)">1366.8</div></div>
</div>
<div class="note">z 임계가 실제로 가리키는 raw 값: <b>z=2 → ae_raw 127.1</b> · <b>z=3 → ae_raw 167.0</b>.
정상 중앙값 33의 4~5배 지점입니다.</div>
</div>

<h2><span class="num">3</span>ae_raw → ae_score — 앵커 변환 (여기서 포화 발생)</h2>
<p class="sub">VAL 정상 분위수 5점에 고정한 단조 선형보간. P99.9(raw 399)에서 1.0에 닿고 <b>그 위는 전부 1.0</b>.</p>
<div class="card"><svg id="curve" viewBox="0 0 900 330"></svg>
<div class="lgd"><span><i style="background:var(--sc)"></i>변환 곡선</span>
<span><i style="background:var(--ink)"></i>앵커 5점</span>
<span><i style="background:var(--dr)"></i>포화 영역 (raw&gt;399 → 전부 1.0)</span></div>
</div>
<div class="note"><b>포화의 실체</b> — 실데이터에서 ae_raw가 399를 넘은 wafer는
<b id="satn"></b>장. 이들의 raw는 399~1366.8로 <b>3.4배까지 벌어지는데 ae_score는 모두 1.0</b>입니다.
z 경로를 권장한 이유가 이 구간입니다.</div>

<h2><span class="num">4</span>ae_score → EWMA → ae_drift_score</h2>
<p class="sub"><code>E_t = 0.05·s_t + 0.95·E_{t−1}</code> → <code>clip((E−0.130)/(0.20−0.130), 0, 1)</code>.
유효 밴드폭이 <b>0.07</b>뿐이라 EWMA가 조금만 올라도 1.0으로 붙습니다.</p>
<div class="card"><svg id="drift" viewBox="0 0 900 300"></svg>
<div class="lgd"><span><i style="background:var(--sc)"></i>EWMA(ae_score)</span>
<span><i style="background:var(--dr)"></i>ae_drift_score</span>
<span><i style="background:#9ca3af"></i>B0=0.130 / ALARM=0.200</span></div>
</div>

<h2><span class="num">5</span>전체 스트림 — 15,919 wafer</h2>
<div class="card"><svg id="stream" viewBox="0 0 900 420"></svg>
<div class="lgd"><span><i style="background:var(--raw)"></i>ae_raw</span>
<span><i style="background:var(--sc)"></i>ae_score</span>
<span><i style="background:var(--dr)"></i>ae_drift_score</span>
<span>세로 점선 = 레짐 전환(seg2 시작, 4,903번째)</span></div>
</div>

<h2><span class="num">6</span>축 기여점수 — 설계서 §3 산식 적용</h2>
<p class="sub">Anomaly(z버킷) <code>z&lt;2→0 / 2≤z&lt;3→+15 / z≥3→+25</code> ·
Drift(밴드) <code>≤0.3→0 / ~0.7→+10 / &gt;0.7→+20</code> · <code>score_ae = max(둘)</code></p>
<div class="card"><svg id="bars" viewBox="0 0 900 240"></svg></div>

<h2><span class="num">7</span>레짐 전환 앞뒤 40장 — 전 단계 실값</h2>
<p class="sub">행을 클릭하면 위 1번 파이프라인이 그 wafer로 바뀝니다.</p>
<div class="scroll"><table id="tbl"></table></div>

<h2><span class="num">8</span>이 데이터가 말해주는 것</h2>
<div class="card"><div class="kv">
<div><div class="k">ae_score = 1.0 포화 wafer</div><div class="v" id="s1"></div></div>
<div><div class="k">└ 정상 레짐(seg1)에서</div><div class="v" id="s2"></div></div>
<div><div class="k">drift = 1.0 고착 wafer</div><div class="v" id="s3"></div></div>
<div><div class="k">max 집계 평균</div><div class="v" id="s4"></div></div>
<div><div class="k">단순합산 평균</div><div class="v" style="color:var(--dr)" id="s5"></div></div>
</div>
<div class="note" style="margin-top:16px">
<b>① Drift는 한 번 1.0에 닿으면 돌아오지 않습니다.</b>
최초 도달 4,913번째 wafer 이후 <b>11,006장 끝까지 고착</b> — 0으로 복귀한 적이 0회.
"0이 다시 오면 재추적"이 실데이터에서 성립하지 않는 이유입니다. 리셋 경계는 값이 아니라 PM입니다.<br><br>
<b>② 정상 레짐에서도 ae_score=1.0이 나옵니다.</b>
seg1(정상)에서 <b id="s6"></b>장. 예: C64_5367은 raw 483.5 · score 1.0인데 drift는 0.0 —
"순간 스파이크 = Anomaly만"의 교과서 사례이자, 엣지 래치가 잘못 걸릴 수 있는 지점입니다.<br><br>
<b>③ 두 채널 선형합산은 실제로 72% 과대계상입니다.</b>
max 16.70 vs 합산 28.65. 그룹핑 문서의 A3 집계 규칙이 실측으로 확인됩니다.<br><br>
<b>④ 상보 관계도 실측으로 확인됩니다.</b>
Drift만 발화(drift=1.0인데 z&lt;2) <b>1,149장</b> · Anomaly만 발화(z≥3인데 drift≤0.3) <b>81장</b>.
두 채널이 서로 다른 것을 잡고 있다는 뜻이고, 그래서 하나로 못 합치되 하나를 버릴 수도 없습니다.
</div></div>

<p class="small" style="margin-top:34px">
재현 코드: <code>ae_numpy_shim.py</code>(torch·sklearn 없는 환경용 numpy 재구현) ·
<code>run_chain.py</code> · <code>run_axes.py</code> ·
데이터 <code>Data/문제1(하)</code> train+valid+test 병합(원 merged_data_v3 재구성) ·
번들 <code>artifacts/</code> model_seed42.pt · scaler.pkl · calib.json
</p>
</div>

<script>
const D = __DATA__;
const S = D.stats, AX = S.anchors_x, AY = S.anchors_y;
const $ = q => document.querySelector(q);
const NS = 'http://www.w3.org/2000/svg';
function el(n, a, p){const e=document.createElementNS(NS,n);
  for(const k in a) e.setAttribute(k,a[k]); if(p)p.appendChild(e); return e;}
function txt(p,x,y,s,o={}){const t=el('text',Object.assign({x,y,'font-size':o.fs||11,
  fill:o.c||'#6b6b66','text-anchor':o.a||'start','font-weight':o.w||400},o.extra||{}),p);
  t.textContent=s; return t;}

/* ---------- 1. 파이프라인 ---------- */
let cur = 0;
function pipe(w){
  const st=[
    ['입력','83 피처','wafer 1장 · Step4 정착/과도',''],
    ['① ae_raw', w.ae_raw.toFixed(1), '(r−μ)ᵀP(r−μ)', 'var(--raw)'],
    ['z 표준화', (w.z>=0?'+':'')+w.z.toFixed(2)+'σ', '(raw−47.31)/39.91', 'var(--raw)'],
    ['② ae_score', w.ae_score.toFixed(3), 'ECDF 앵커 보간 [0,1]', 'var(--sc)'],
    ['EWMA', w.ewma.toFixed(4), 'α=0.05 누적', 'var(--sc)'],
    ['③ drift', w.ae_drift_score.toFixed(3), '(E−0.13)/0.07', 'var(--dr)'],
  ];
  let h = st.map(s=>`<div class="stage"><div class="lb">${s[0]}</div>
    <div class="vl" style="color:${s[3]||'var(--ink)'}">${s[1]}</div>
    <div class="fm">${s[2]}</div></div>`).join('');
  h += `<div class="stage" style="background:#f6f5f3;border-color:#d6d3cd">
    <div class="lb">축 기여점수</div>
    <div class="vl">${w.score_ae_z}점</div>
    <div class="fm">max(A ${w.anom_z}, D ${w.drift_pts})</div></div>`;
  $('#pipe').innerHTML = h;
  $('#pipenote').innerHTML = `<b>${w.wafer}</b>${w.desc?' — '+w.desc:''} ·
    단순합산이라면 ${w.score_ae_sum}점 (max 대비 +${w.score_ae_sum-w.score_ae_z}점 과대)`;
  drawCurve(w); drawDrift(w);
}
$('#btns').innerHTML = D.ex.map((e,i)=>
  `<button class="btn${i==0?' on':''}" data-i="${i}"><b>${e.label}</b><span>${e.desc}</span></button>`).join('');
$('#btns').onclick = e => {const b=e.target.closest('.btn'); if(!b)return;
  document.querySelectorAll('#btns .btn').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); cur=+b.dataset.i; pipe(D.ex[cur]); markTbl(D.ex[cur].wafer);};

/* ---------- 3. 변환 곡선 ---------- */
function drawCurve(w){
  const s=$('#curve'); s.innerHTML='';
  const L=58,R=880,T=18,B=278, xmax=700;
  const X=v=>L+Math.min(v,xmax)/xmax*(R-L), Y=v=>B-v*(B-T);
  el('rect',{x:X(399),y:T,width:R-X(399),height:B-T,fill:'#fee2e2',opacity:.55},s);
  txt(s,X(399)+9,T+16,'포화 영역 — raw가 아무리 커도 score = 1.0',{c:'#b91c1c',fs:11.5,w:600});
  [0,.2,.5,1].forEach(v=>{el('line',{x1:L,y1:Y(v),x2:R,y2:Y(v),stroke:'#eceae6'},s);
    txt(s,L-9,Y(v)+4,v.toFixed(1),{a:'end'});});
  for(let v=0;v<=xmax;v+=100){el('line',{x1:X(v),y1:B,x2:X(v),y2:B+4,stroke:'#c9c6c0'},s);
    txt(s,X(v),B+18,v,{a:'middle'});}
  txt(s,(L+R)/2,B+38,'ae_raw (조건화 Maha²)',{a:'middle',fs:12});
  txt(s,L-40,T-4,'ae_score',{fs:12});
  let d=`M ${X(0)} ${Y(0)}`;
  AX.forEach((x,i)=>d+=` L ${X(x)} ${Y(AY[i])}`);
  d+=` L ${X(xmax)} ${Y(1)}`;
  el('path',{d,fill:'none',stroke:'var(--sc)','stroke-width':2.6},s);
  AX.forEach((x,i)=>{el('circle',{cx:X(x),cy:Y(AY[i]),r:4.5,fill:'#1a1a18'},s);
    txt(s,X(x),Y(AY[i])-11,`P${S.anchors_x?['50','90','98.5','99.5','99.9'][i]:''}`,
      {a:'middle',fs:10,c:'#1a1a18',w:600});
    txt(s,X(x),Y(AY[i])+16,x.toFixed(0),{a:'middle',fs:9.5});});
  const px=X(w.ae_raw),py=Y(w.ae_score);
  el('line',{x1:px,y1:B,x2:px,y2:py,stroke:'var(--warn)','stroke-dasharray':'3 3'},s);
  el('line',{x1:L,y1:py,x2:px,y2:py,stroke:'var(--warn)','stroke-dasharray':'3 3'},s);
  el('circle',{cx:px,cy:py,r:6,fill:'var(--warn)',stroke:'#fff','stroke-width':2},s);
  txt(s,Math.min(px+11,R-160),py-9,`${w.wafer}: ${w.ae_raw.toFixed(1)} → ${w.ae_score.toFixed(3)}`,
    {c:'#92400e',fs:11.5,w:600});
}

/* ---------- 4. drift 구간 확대 ---------- */
function drawDrift(w){
  const s=$('#drift'); s.innerHTML='';
  const st=D.stream, L=58,R=880,T=18,B=250;
  const i0=Math.max(0,S.seg2_start-600), i1=Math.min(S.n,S.seg2_start+1400);
  const idx=st.i.map((v,k)=>k).filter(k=>st.i[k]>=i0&&st.i[k]<=i1);
  const X=v=>L+(v-i0)/(i1-i0)*(R-L);
  const Ye=v=>B-Math.min(v,.32)/.32*(B-T);
  [[0.130,'B0 = 0.130'],[0.200,'ALARM = 0.200']].forEach(([v,lb])=>{
    el('line',{x1:L,y1:Ye(v),x2:R,y2:Ye(v),stroke:'#9ca3af','stroke-dasharray':'5 4'},s);
    txt(s,R+4,Ye(v)+4,lb,{fs:10.5});});
  el('rect',{x:L,y:Ye(.2),width:R-L,height:Ye(.13)-Ye(.2),fill:'#dbeafe',opacity:.5},s);
  txt(s,L+8,Ye(.165)+4,'유효 밴드폭 0.07',{fs:10.5,c:'#1d4ed8',w:600});
  let de='',dd='';
  idx.forEach((k,n)=>{de+=(n?' L ':'M ')+X(st.i[k])+' '+Ye(st.ew[k]);
    dd+=(n?' L ':'M ')+X(st.i[k])+' '+(B-st.dr[k]*(B-T));});
  el('path',{d:dd,fill:'none',stroke:'var(--dr)','stroke-width':2,opacity:.85},s);
  el('path',{d:de,fill:'none',stroke:'var(--sc)','stroke-width':2.2},s);
  el('line',{x1:X(S.seg2_start),y1:T,x2:X(S.seg2_start),y2:B,stroke:'#1a1a18',
    'stroke-dasharray':'4 4'},s);
  txt(s,X(S.seg2_start)+6,T+13,'레짐 전환',{c:'#1a1a18',fs:11,w:600});
  el('line',{x1:X(D.first1),y1:T,x2:X(D.first1),y2:B,stroke:'var(--dr)','stroke-dasharray':'2 3'},s);
  txt(s,X(D.first1)+6,T+30,`drift 최초 1.0 (${D.first1}번째)`,{c:'var(--dr)',fs:11,w:600});
  el('line',{x1:L,y1:B,x2:R,y2:B,stroke:'#c9c6c0'},s);
  txt(s,L,B+20,`wafer #${i0}`,{fs:11}); txt(s,R,B+20,`#${i1}`,{a:'end',fs:11});
  txt(s,L-9,B-((B-T))+4,'1.0',{a:'end'}); txt(s,L-9,B+4,'0',{a:'end'});
  if(w.i>=i0&&w.i<=i1){el('circle',{cx:X(w.i),cy:B-w.ae_drift_score*(B-T),r:5.5,
    fill:'var(--warn)',stroke:'#fff','stroke-width':2},s);}
  txt(s,L+8,T+13,'파란선 = EWMA(좌 눈금 0~0.32) · 빨간선 = drift(0~1)',{fs:11});
}

/* ---------- 5. 전체 스트림 ---------- */
(function(){
  const s=$('#stream'); const st=D.stream, L=58,R=880;
  const rows=[['ae_raw','raw','var(--raw)',700],['ae_score','sc','var(--sc)',1],
              ['ae_drift_score','dr','var(--dr)',1]];
  rows.forEach((r,ri)=>{
    const T=14+ri*138, B=T+108;
    const Y=v=>B-Math.min(v,r[3])/r[3]*(B-T);
    el('rect',{x:L,y:T,width:R-L,height:B-T,fill:'#fff',stroke:'#eceae6'},s);
    let d='';
    st[r[1]].forEach((v,k)=>{d+=(k?' L ':'M ')+(L+k/(st.i.length-1)*(R-L))+' '+Y(v);});
    el('path',{d,fill:'none',stroke:r[2],'stroke-width':1.2,opacity:.9},s);
    const xb=L+(S.seg2_start/S.n)*(R-L);
    el('line',{x1:xb,y1:T,x2:xb,y2:B,stroke:'#1a1a18','stroke-dasharray':'4 4'},s);
    txt(s,L+7,T+15,r[0],{fs:12,w:600,c:r[2]});
    txt(s,L-9,T+5,r[3]===1?'1.0':'700',{a:'end'}); txt(s,L-9,B+4,'0',{a:'end'});
    if(ri===0)txt(s,xb+6,T+15,'레짐 전환 (4,903)',{fs:11,c:'#1a1a18',w:600});
    if(ri===2){txt(s,L,B+20,'wafer #0 (시간순)',{fs:11});
      txt(s,R,B+20,'#15,919',{a:'end',fs:11});}
  });
})();

/* ---------- 6. 버킷 막대 ---------- */
(function(){
  const s=$('#bars'); const L=58,R=880;
  const A=[['z < 2','0점',0,'#e5e3df'],['2 ≤ z < 3','+15점',15,'#fcd34d'],['z ≥ 3','+25점',25,'#f59e0b']];
  const Dr=[['d ≤ 0.3','0점',0,'#e5e3df'],['0.3 < d ≤ 0.7','+10점',10,'#fca5a5'],['d > 0.7','+20점',20,'#ef4444']];
  [[A,'Anomaly 채널 (z 버킷)',14],[Dr,'Drift 채널 (밴드)',124]].forEach(([g,lb,T])=>{
    txt(s,L,T+2,lb,{fs:12.5,w:600,c:'#1a1a18'});
    g.forEach((b,i)=>{const x=L+i*250, y=T+16, h=Math.max(b[2]/25*56,4);
      el('rect',{x,y:y+56-h,width:210,height:h,fill:b[3],rx:4},s);
      txt(s,x,y+74,b[0],{fs:12,w:600,c:'#1a1a18'});
      txt(s,x+210,y+74,b[1],{a:'end',fs:12,w:700,c:'#1a1a18'});});
  });
  txt(s,L,232,'score_ae = max(Anomaly, Drift) — 두 채널이 단일 ae_raw 파생이라 합산 금지 (그룹핑 A3)',
    {fs:12,c:'#6b6b66'});
})();

/* ---------- 7. 테이블 ---------- */
(function(){
  const cols=[['wafer','wafer'],['seg','구간'],['ae_raw','ae_raw'],['z','z'],
    ['ae_score','ae_score'],['ewma','EWMA'],['ae_drift_score','drift'],
    ['anom_z','A(z)'],['drift_pts','D'],['score_ae_z','score_ae'],['score_ae_sum','합산시']];
  let h='<thead><tr>'+cols.map(c=>`<th>${c[1]}</th>`).join('')+'</tr></thead><tbody>';
  D.tbl.forEach(r=>{
    h+=`<tr data-w="${r.wafer}" class="${r.seg=='seg2'?'seg2':''}" style="cursor:pointer">`+
      cols.map(c=>{let v=r[c[0]];
        if(typeof v==='number') v=(c[0]=='ae_raw')?v.toFixed(1):
          (['z','ae_score','ewma','ae_drift_score'].includes(c[0])?v.toFixed(3):v);
        const em=(c[0]=='score_ae_z')?'font-weight:700':
          (c[0]=='score_ae_sum'?'color:#dc2626':'');
        return `<td style="${em}">${v}</td>`;}).join('')+'</tr>';});
  $('#tbl').innerHTML=h+'</tbody>';
  $('#tbl').onclick=e=>{const tr=e.target.closest('tr[data-w]'); if(!tr)return;
    const r=D.tbl.find(x=>x.wafer==tr.dataset.w);
    const i=D.tbl.indexOf(r)+S.seg2_start-10;
    pipe(Object.assign({},r,{i,desc:''})); markTbl(r.wafer);
    document.querySelectorAll('#btns .btn').forEach(x=>x.classList.remove('on'));};
})();
function markTbl(w){document.querySelectorAll('#tbl tr').forEach(t=>
  t.classList.toggle('hl',t.dataset&&t.dataset.w==w));}

/* ---------- 숫자 채우기 ---------- */
$('#mu').textContent=S.mu_raw.toFixed(2); $('#sd').textContent=S.sd_raw.toFixed(2);
$('#satn').textContent=D.n_sat_score.toLocaleString();
$('#s1').textContent=D.n_sat_score.toLocaleString()+'장';
$('#s2').textContent=D.seg1_sat+'장';
$('#s3').textContent=D.n_sat_drift.toLocaleString()+'장';
$('#s4').textContent=D.mean_max.toFixed(2)+'점';
$('#s5').textContent=D.mean_sum.toFixed(2)+'점';
$('#s6').textContent=D.seg1_sat;
pipe(D.ex[0]);
</script></body></html>"""

(OUT / "AE_스코어체인_실데이터.html").write_text(
    HTML.replace("__DATA__", json.dumps(D, ensure_ascii=False)), encoding="utf-8")
print("written")
