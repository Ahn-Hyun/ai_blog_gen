# AI 자동화 블로그 생성기 개발 계획

## 1. 목표
- 전 세계/국내 핫 키워드 수집 후 주제 선별
- AI로 글 생성 + 관련 이미지 검색 + hero 이미지(나노바나나) 생성
- `ai_blog_v1_astro`에 MDX/이미지 자동 반영 후 GitHub 업데이트
- Cloudflare Pages가 GitHub 업데이트를 감지해 자동 배포

## 2. 범위(MVP)
- 트렌드 키워드 수집(2~3개 소스) 및 중복 제거
- 키워드 → 주제/아웃라인 → 본문 생성
- 이미지 검색 1~2개 소스 + hero 이미지 생성
- Astro 컨텐츠 스키마에 맞는 MDX 생성
- GitHub 커밋/푸시 자동화

## 3. 제약/가정
- 블로그 배포는 Astro + Cloudflare Pages
- 콘텐츠 업데이트는 GitHub 커밋/푸시로만 수행
- Astro 블로그는 `src/content/blog/`의 MDX와
  `public/images/posts/<slug>/hero.jpg` 구조를 유지

## 4. 시스템 구성(상위)
- Trend Collector: 키워드 수집
- Topic Ranker: 중요도/중복/신선도 점수화
- Content Generator: 아웃라인 + 본문 생성
- Image Pipeline: 이미지 검색 + 나노바나나 hero 생성
- Content Formatter: Astro 스키마 맞춘 MDX 작성
- Publisher: git commit/push 및 메타 기록
- State Store: 처리 이력/중복 방지(로컬 DB/JSON)

## 5. 데이터 흐름
1) 트렌드 수집 → 2) 키워드 정제/중복 제거
3) 주제 선별(점수화) → 4) 아웃라인 생성
5) 본문 생성 → 6) 품질/중복 체크
7) 이미지 검색 + hero 생성
8) MDX/이미지 저장 → 9) git commit/push
10) Cloudflare Pages 자동 빌드/배포

## 6. Astro 출력 규격(핵심)
`ai_blog_v1_astro/src/content.config.ts` 기준 필수/권장 필드:
- 필수: `title`, `description`, `pubDate`
- 권장: `updatedDate`, `category`, `tags`, `draft`
- heroImage: `src`, `alt`
- seo: `canonical`, `ogTitle`, `ogDescription`

MDX frontmatter 예시:
```
---
title: "..."
description: "..."
pubDate: 2026-01-27
updatedDate: 2026-01-27
category: ["ai", "trends"]
tags: ["AI", "Trend"]
draft: false
heroImage:
  src: "/images/posts/2026-01-27-sample/hero.jpg"
  alt: "..."
seo:
  canonical: "https://<domain>/blog/2026-01-27-sample"
  ogTitle: "..."
  ogDescription: "..."
---
```

## 7. 이미지 전략
- 검색 이미지: Pexels/Unsplash 등 라이선스 준수 소스 사용
- hero 이미지: 나노바나나 모델/서비스 API 사용
- 이미지 출처 기록(본문 하단 또는 별도 메타)

## 8. 디렉터리 구조 제안(ai_blog_v1_AI)
```
ai_blog_v1_AI/
  src/
    collectors/        # 트렌드 소스 커넥터
    ranking/           # 키워드 점수화/필터
    generation/        # 아웃라인/본문 프롬프트
    images/            # 이미지 검색/hero 생성
    formatter/         # MDX/slug 생성
    publisher/         # git commit/push
    store/             # 처리 이력 저장
  data/
  logs/
  config/
  DEV_PLAN.md
```

## 9. 자동화/운영
- 스케줄: GitHub Actions cron 또는 별도 서버 크론
- 재시도/백오프: API 실패 시 지수 백오프
- 비용 관리: 호출량/토큰 사용량 상한
- 안전장치: 중복 주제 방지, 길이 제한, 키워드 블랙리스트

## 10. 품질/검수
- 중복/유사도 검사(제목/요약/본문)
- 사실성 최소 검증(간단한 출처 확인 또는 신뢰 소스 우선)
- `draft: true`로 자동 게시 후 수동 승인 옵션

## 11. 로드맵(단계)
1) 폴더/설정/스켈레톤 구성
2) 트렌드 수집기 1~2개 연결
3) 키워드 정제 + 주제 선정 로직
4) 아웃라인/본문 생성 + 품질 게이트
5) 이미지 검색 + hero 생성 연동
6) MDX/이미지 출력 + git publish
7) 스케줄링/모니터링/알림

## 12. 결정 필요 사항
- 트렌드 소스(국내/해외) 우선순위
- 나노바나나 API 방식/요금제
- 이미지 라이선스 정책
- 자동 게시 vs 검수 후 게시 정책
