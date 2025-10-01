# Rules for coreadr



## Directory Structure  
Follow and extend this structure when adding new modules:

/
.
├── doc
│   ├── plan
│   │   └── project_idea.md
│   └── rules
│       ├── create-prd.md
│       ├── generate-tasks.md
│       ├── process-task-list.md
│       └── rules.md
├── memory
│   └── agent_guides
│       ├── Phaedrus.md
│       ├── Plato.md
│       ├── Socrates.md
│       └── Xenophon.md
├── rc
│   ├── agents
│   │   ├── base.py
│   │   ├── phaedrus.py
│   │   ├── plato_llm.py
│   │   ├── plato.py
│   │   ├── socrates_llm.py
│   │   ├── socrates.py
│   │   └── xenophon.py
│   ├── cli.py
│   ├── core
│   │   ├── events.py
│   │   ├── openai_client.py
│   │   ├── paths.py
│   │   ├── simulator.py
│   │   └── state.py
│   ├── __init__.py
│   └── utils
│       └── markdown.py
├── README.md
├── requirements.txt
├── run_simulate.bat
├── run_streamlit.bat
└── streamlit_app.py



## Rules & Constraints

6. Do not change `.github/workflows/` without prior approval (CI/CD may break).  
8. Shared helpers/utilities must go into `lib/`. Avoid duplication.  
9. e2e and integration tests belong in `tests/`. Unit tests may go alongside the component or in `tests/unit/`.  
10. Styling / Tailwind / Radix primitives must be centralized for consistent reuse.  

## Naming Conventions

11. Directories — kebab-case (lowercase, words separated by dashes).  
12. Component / page files — PascalCase (`MyComponent.tsx`, `HomePage.tsx`).  
13. Utilities / hooks — camelCase (`useChatHistory`, `formatDate`).  
14. Configuration files — `.config.ts` / `.config.mjs` / `.json`, consistent with existing files (`tsconfig.json`, `postcss.config.mjs`, `drizzle.config.ts`).  

## Environment & Secrets

15. Never commit real secrets in `.env.*`. Use `.env.example` as a template.  
16. Any new environment variables must be documented in the README.  

## Rule Enforcement

17. Before starting a feature, verify that existing routes / APIs are not broken, especially under `app/api`.  
18. If changing files in `app/`, `components/`, or `lib/`, PRs must include which parts of the tree were modified and why. Update the **Directory Structure** section if the structure itself changes.  
19. The AI assistant should always read `README.md` and the current directory structure before suggesting changes, to avoid breaking the established architecture.  

