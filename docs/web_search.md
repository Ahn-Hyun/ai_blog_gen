강의 생성에서 웹 검색은 **Tavily 기반 도구(`searchWeb`, `extractWebContent`)를 Gemini 에이전트가 호출**하는 방식으로 동작합니다. 실제로 섹션 생성 단계의 `generateText` 도구 목록에 웹 검색 도구가 포함되어 있어, 필요 시 모델이 직접 검색→원문 추출을 수행합니다.  
```2298:2321:apps/server/src/lib/ai/lesson-generator/services/content-generator.service.ts
        const result = await generateText({
          model,
          messages,
          temperature: isCorrection ? 0.3 : 0.5, // Lower temperature for corrections
          maxRetries: 2,
          maxSteps: 20,
          tools: isCorrection
            ? {
                // ...tavilyTools,
                searchKnowledgeBase: searchKnowledgeBaseTool,
                searchYouTube: youtubeSearchTool,
                searchWikipediaImage: searchWikipediaImageTool,
                searchWeb: webSearch,
                extractWebContent: webExtract,
              }
            : {
                // ...tavilyTools,
                generateImage: generateImageTool,
                searchWikipediaImage: searchWikipediaImageTool,
                generateAudio: generateAudioTool,
                searchKnowledgeBase: searchKnowledgeBaseTool,
                searchYouTube: youtubeSearchTool,
                searchWeb: webSearch,
                extractWebContent: webExtract,
              },
        });
```

### 실제 사용 흐름(권장 규칙)
- **`searchWeb`로 최신/신뢰 가능한 소스 탐색**
- **특정 결과를 사용할 땐 반드시 `extractWebContent`로 원문 추출 후 반영**
- **정책/법/버전처럼 시의성이 중요한 주제는 웹 검색이 필수**
```2111:2131:docs/lesson-generation-process-prompts.md
TOOL USAGE - WEB RESEARCH (TAVILY SEARCH & EXTRACT):
You have two complementary Tavily-powered tools for finding and using current, credible web information:

1. searchWeb (Tavily Search API)
   • When to use:
     - **MANDATORY for time-sensitive topics** (e.g., visa policies, laws, technology versions, medical guidelines).
     - To discover recent developments, statistics, or reputable sources not present in the course materials.
     - To verify if information in your knowledge base might be outdated (e.g., "Country X visa policies 2025").
...
2. extractWebContent (Tavily Extract API)
   • When to use: after searchWeb identifies promising URLs and you need the underlying article text for accurate summarization or quotation.
...
- searchWeb only returns summaries/previews. If you decide to rely on a specific result, you MUST immediately follow up with extractWebContent for that exact URL before quoting, summarizing, or embedding details. Never fabricate or infer unverified content from the search preview alone.
```

### 구현/설정 포인트
- 도구 자체는 **Tavily API를 직접 호출**하며 `TAVILY_API_KEY`가 없으면 실패합니다.
```79:86:apps/server/src/lib/ai/tools/web.tool.ts
    const apiKey = process.env.TAVILY_API_KEY;

    if (!apiKey) {
      return {
        success: false,
        error:
          "Tavily API key is not configured. Set the TAVILY_API_KEY environment variable.",
      } satisfies WebSearchToolOutput;
    }
```

### 문서화된 워크플로
- 생성 파이프라인에서 **`webSearch`, `extractWebContent`가 도구로 포함됨**을 명시합니다.
```116:122:docs/lesson-generation-workflow.md
2) Content generation:
   - Builds a section-specific prompt that includes objectives, length guidance, and recommended MDX components.
   - Uses `generateText` with tool support:
     - `generateImage`, `generateAudio`
     - `searchKnowledgeBase`, `searchYouTube`, `searchWikipediaImage`
     - `webSearch`, `extractWebContent`
```

[2 tools called]

아래는 **강의 생성 중 웹 검색을 실제로 쓰는 예시 흐름**을 간단히 정리한 거야. (도구는 `searchWeb` → `extractWebContent` 순서로 쓰는 게 원칙)

## 예시: 최신 정책/버전 확인

### 1) `searchWeb` 호출 예시  
```json
{
  "query": "2026년 기준 유럽 AI 규제 시행 일정 최신",
  "searchDepth": "basic",
  "maxResults": 4,
  "includeAnswer": true
}
```

### 2) 유효한 결과 URL 선택 후 `extractWebContent` 호출  
```json
{
  "urls": [
    "https://example.gov/ai-regulation-timeline-2026"
  ],
  "maxCharacters": 6000
}
```

### 3) 강의 본문(MDX) 반영 예시  
> **주의:** 대괄호형 인용([1])은 금지. 자연스럽게 출처를 언급.
```mdx
최근 유럽연합 공식 발표(2025-11-30 발행)에 따르면, AI 규제는 단계적으로 시행되며
고위험 시스템에 대한 의무 요건이 먼저 적용된다. 따라서 실무 적용 시점은 제품 유형에 따라
상이할 수 있다.
```

## 팁/주의사항
- **시의성 높은 주제(법/정책/버전)**는 `searchWeb`이 **필수**.
- `searchWeb`은 요약만 반환하므로, **실제 반영 전에는 반드시 `extractWebContent`로 원문 확인**.
- 신뢰도 높은 출처가 필요하면 `includeDomains`로 `gov`, `edu`, `org` 등으로 제한 가능.
- 환경 변수 `TAVILY_API_KEY`가 없으면 검색 도구가 실패.

