{#- Terms that mark a job description as mentioning generative AI (version 1, 2026-11-02).

    Each entry: the label reported, a PostgreSQL regular expression, and whether case matters.
    \m and \M are word boundaries. A bare "AI" is deliberately not a term: it appears in
    company names (Shield AI, Indicium AI) and in boilerplate, and would count almost every
    listing. The employer's own name is also removed from the text before matching
    (int_genai_mentions). Changing this list changes the reported numbers from the next build
    on, for every week, so change it deliberately and note it in the dashboard caption. -#}
{% macro genai_terms() %}
    {{ return([
        {'term': 'LLM', 'pattern': '\\mLLMs?\\M', 'case_sensitive': true},
        {'term': 'large language model', 'pattern': '\\mlarge language models?\\M', 'case_sensitive': false},
        {'term': 'generative AI', 'pattern': '\\mgenerative (ai|artificial intelligence)\\M|\\mgen ?ai\\M', 'case_sensitive': false},
        {'term': 'RAG', 'pattern': '\\mRAG\\M', 'case_sensitive': true},
        {'term': 'retrieval-augmented generation', 'pattern': '\\mretrieval[- ]augmented generation\\M', 'case_sensitive': false},
        {'term': 'AI agents', 'pattern': '\\mAI agents?\\M|\\magentic\\M', 'case_sensitive': false},
        {'term': 'prompt engineering', 'pattern': '\\mprompt engineer(ing)?\\M', 'case_sensitive': false},
        {'term': 'ChatGPT / OpenAI', 'pattern': '\\mchatgpt\\M|\\mopenai\\M|\\mGPT-?[345]', 'case_sensitive': false},
        {'term': 'coding assistant (Copilot)', 'pattern': '\\m(github|microsoft|m365) copilot\\M|\\mcopilot studio\\M', 'case_sensitive': false},
        {'term': 'LangChain / LlamaIndex', 'pattern': '\\mlangchain\\M|\\mllama ?index\\M', 'case_sensitive': false},
        {'term': 'vector database', 'pattern': '\\mvector (database|db|store|search)s?\\M', 'case_sensitive': false},
        {'term': 'foundation models', 'pattern': '\\mfoundation models?\\M', 'case_sensitive': false},
    ]) }}
{% endmacro %}
