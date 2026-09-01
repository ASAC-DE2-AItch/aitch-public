# =============================================================================
# frontend — React 정적 빌드 + nginx 진입 (인프라 스텝 1)
#
# ⚠ `frontend/` 는 PM/PO 소유(헌법 3-1, 2026-07-31 등재). PR #121 에서 컨테이너화 전체를
#   팀원 C 주도로 위임받은 범위에서 작성했다. **리뷰는 PM.**
#   **`frontend/src/` 는 한 글자도 건드리지 않는다** — 신규 파일 3개(Dockerfile·.dockerignore·
#   nginx/default.conf.template)뿐이라 `feat/front-intro-overview` 작업과 충돌하지 않는다
#   (그 브랜치가 고치는 21파일은 전부 `src/`·`tools/` 이고 빌드 설정은 무변경 — 대조 확인함).
#
# 빌드는 **frontend/ 를 컨텍스트로** 한다 (다른 이미지와 다르다):
#     docker build -f frontend/Dockerfile -t aitch-frontend:p1 ./frontend
#   리포 루트를 컨텍스트로 잡을 이유가 없고(프론트는 파이썬 코드를 안 본다), 루트
#   `.dockerignore` 가 `frontend/` 를 통째로 제외하고 있어 그쪽으론 애초에 못 넣는다.
#
# ── 2단 빌드 ────────────────────────────────────────────────────────────────
#   ① node  — `npm ci && npm run build` → dist/
#   ② nginx — dist/ 를 서빙 + `/api/*` 를 게이트웨이로 프록시 (이미지에 node 를 남기지 않는다)
#
# ── ⚠️ 알아둘 것 두 가지 ────────────────────────────────────────────────────
#   ① **빌드 실패 = 대개 프론트 코드의 타입 에러이지 이 Dockerfile 문제가 아니다.**
#      `package.json` 의 build 가 `tsc -b && vite build` 라 **타입체크가 빌드에 포함**돼 있다.
#      리팩터 중인 브랜치를 빌드하면 여기서 멈출 수 있다 — 로그의 `tsc` 출력을 먼저 볼 것.
#
#   ② **`vite.config.js` 그림자 검사**(아래 RUN). Vite 는 설정 파일을 찾을 때 `.js` 를 `.ts`
#      보다 **먼저** 로드하므로, `.js` 가 생기면 플러그인(react·tailwind) 없는 부실 설정이
#      정본 `.ts` 를 조용히 가린다 → 목업만 나오는 화면. 로컬에서 두 번 밟은 자리다
#      (2026-07-22 삭제 → 07-24 재발, 헌법 7장). 컨테이너에서는 **빌드를 세워** 앞당긴다.
# =============================================================================

# ── ① 빌드 단계 ──────────────────────────────────────────────────────────────
FROM node:20-alpine AS build
# node 20 — vite 5 · tailwind 4 요구 하한(18/20). 팀 로컬과 major 를 맞춰 둔다.

WORKDIR /app

# 의존성 먼저 — 소스만 고칠 때 npm ci 레이어를 캐시로 넘긴다.
# `npm ci` 는 package-lock.json 을 그대로 따른다(`install` 과 달리 lock 을 고쳐 쓰지 않음) —
# 이미지가 로컬과 같은 트리를 갖게 하는 재현성 장치다.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# 헌법 7장 — vite.config.js 그림자 차단 (위 ②)
RUN if [ -f vite.config.js ]; then \
      echo "ERROR: vite.config.js 가 존재한다 — Vite 가 정본 vite.config.ts 대신 이 파일을" >&2; \
      echo "       먼저 로드해 react()·tailwindcss() 플러그인 없이 빌드한다(헌법 7장)." >&2; \
      echo "       삭제하고 다시 빌드할 것. 증상: 화면이 목업만 나옴." >&2; \
      exit 1; \
    fi

RUN npm run build

# ── ② 서빙 단계 ──────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine

# 게이트웨이 위치 — 템플릿의 ${GATEWAY_UPSTREAM} 로 치환된다(nginx 공식 이미지의 envsubst).
# 기본값은 컨테이너화된 게이트웨이. 아직 호스트 프로세스로 띄우는 동안에는 compose 에서
# `host.docker.internal:8000` 으로 덮는다.
ENV GATEWAY_UPSTREAM=gateway:8000

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# CMD 는 베이스 이미지 것을 그대로 쓴다 — nginx 공식 entrypoint 가
# /etc/nginx/templates/*.template 를 envsubst 로 치환한 뒤 nginx 를 exec 한다.
