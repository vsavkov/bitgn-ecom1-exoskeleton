# AGENTS.md

Это репозиторий разрабатываемого агента для челенджа ECOM1 for agentic ecommerce.

Цитата с сайта челенджа

> You write an agent, connect it to BitGN via API, and solve tasks inside a deterministic simulated commercial environment. BitGN evaluates observable outcomes such as tool calls, state changes, required flags/references, and forbidden actions avoided.

Агент работает через API в специальном Sandbox. На вход подаются разные тестовые задачи общения клиента или сотрудника с агентом.
Каждая задача оценивается. По результатам тестам формируется Leaderboard с лучшими результатами.

Ты должен мне помочь разработать агента, который получит максимум очков. 
При этом нужно не затачиваться под конкретные задачи, а выявлять на основе них нюансы и правила поведения,
которые должны быть отражены в агенте.