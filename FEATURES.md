# Jarvis — Feature Catalogue (100 tools)

Every capability Jarvis can invoke, grouped by area. You can trigger any of these
by **voice** ("Jarvis, …"), by **typing** in the HUD, or by **texting/voice-noting**
the Telegram bot. Jarvis chooses the right tool automatically — you just speak naturally.

> Legend: 🌐 needs internet · 🤖 uses a Gemini request (counts toward the daily quota)
> · 💻 local/instant/free · 📷 uses the webcam · ⚠ changes your machine's state

---

## 🗣 Voice & core
1. **get_current_time** — the current local time. 💻
2. **get_current_date** — today's date. 💻
3. **calculate** — evaluate any math expression (powers, sqrt, sin/cos/log…). 💻
4. **run_shell_command** — open native apps / run system commands (e.g. open Notepad, shutdown). ⚠
5. **windows_control** — control a native Windows app via the accessibility API (focus, click, type, read). ⚠

## 🌐 Web & browser automation
6. **open_url** — open a page in your default browser. 🌐
7. **web_search** — live DuckDuckGo search for current info. 🌐
8. **browser_navigate** — drive the automation browser to a URL (logins persist). 🌐
9. **browser_snapshot** — read the page as a numbered list of interactive elements. 🌐
10. **browser_click** — click an element by number (or text). 🌐
11. **browser_type** — type into a field by number (optionally submit). 🌐
12. **browser_key** — press a key in the browser (Enter, Tab, Esc…). 🌐
13. **browser_scroll** — scroll to reveal off-screen content, then re-snapshot. 🌐
14. **browser_tab** — open / list / switch / close browser tabs. 🌐
15. **click_by_vision** — click an unlabeled element by describing it (Set-of-Marks vision). 🌐🤖
16. **read_url** — fetch an article/page and summarise or answer about it. 🌐🤖
17. **deep_research** — research a topic across multiple sources → a cited synthesis. 🌐🤖
18. **shorten_url** — shorten a long link (TinyURL). 🌐
19. **expand_url** — resolve a shortened link to its real destination. 🌐

## 👁 Vision — screen & webcam
20. **look_at_screen** — see and describe/read your desktop screen. 🤖
21. **look_at_page** — visually verify what's on the current browser page. 🌐🤖
22. **describe_image** — describe / answer about a local image file. 🤖
23. **look_through_webcam** — see you through the webcam ("what am I holding?"). 📷🤖
24. **check_posture** — assess your sitting posture from the side webcam. 📷🤖
25. **presence_mode** — toggle always-on webcam presence awareness. 📷💻
26. **camera_recall** — review recent webcam frames ("did anyone come by while I was away?"). 📷🤖
27. **focus_report** — how long you've been at your desk and how often you stepped away. 📷💻
28. **take_photo** — snap and save a photo from the webcam. 📷
29. **scan_qr** — decode a QR code held up to the webcam (local). 📷💻

## 🧠 Memory
30. **remember** — save a durable fact about you to long-term memory. 🤖
31. **recall** — search your long-term memory for related context. 🤖
32. **forget** — delete a stored memory by description. 🤖

## 🖥 Desktop & system control
33. **computer_control** — operate ANY Windows app by sight (click/type in Notepad, Settings, games…). ⚠🤖📷
34. **media_control** — play/pause/next/previous/volume via media keys. ⚠
35. **system_info** — OS, CPU, RAM and GPU summary. 💻
36. **list_processes** — top running processes by memory. 💻
37. **kill_process** — close processes matching a name. ⚠
38. **battery_status** — battery % and charging state. 💻
39. **screenshot_save** — save a screenshot of the desktop to a file. 💻
40. **set_clipboard** — put text onto the clipboard. ⚠
41. **read_clipboard** — read whatever text you've copied. 💻
42. **ip_info** — your public IP and rough location. 🌐

## 📁 Files & folders
43. **list_files** — list a directory's contents. 💻
44. **search_files** — find files by name under a folder. 💻
45. **open_folder** — open a folder in Explorer. ⚠
46. **create_folder** — make a new folder. ⚠
47. **move_path** — move a file/folder. ⚠
48. **copy_path** — copy a file/folder. ⚠
49. **delete_path** — delete safely (moves to a reversible trash folder). ⚠
50. **rename_path** — rename a file/folder. ⚠
51. **zip_path** — zip a file/folder. ⚠
52. **unzip_file** — extract a .zip archive. ⚠
53. **disk_usage** — free/total disk space. 💻
54. **file_info** — size and modified date of a file/folder. 💻
55. **write_file** — create/overwrite a text file or .docx (notes, drafts, code). ⚠
56. **ask_file** — read a local doc (.txt/.md/.csv/.pdf/.docx/code) and answer about it. 🤖

## ⏰ Productivity & automation
57. **set_reminder** — a spoken reminder/timer when it's due (also pushes to phone). 💻
58. **schedule_task** — run a command later or daily, autonomously, and notify you. ⚙
59. **list_scheduled** — list your scheduled/automated tasks. 💻
60. **cancel_scheduled** — cancel a scheduled task. 💻
61. **daily_briefing** — spoken greeting + date + weather + headlines + reminders. 🌐🤖
62. **set_plan** — declare a multi-step plan (shows as a live HUD checklist). 💻
63. **complete_step** — tick off a plan step as it's finished. 💻

## 📱 Communication / phone
64. **notify_phone** — push a message to your phone via Telegram. 🌐

## 📚 Knowledge & live info (free, no API key)
65. **wikipedia_search** — a short Wikipedia summary. 🌐
66. **get_weather** — current weather for a place. 🌐
67. **get_forecast** — multi-day weather forecast. 🌐
68. **air_quality** — air-quality index for a place. 🌐
69. **sunrise_sunset** — sunrise/sunset times for a place. 🌐
70. **get_news** — current news headlines (optionally by topic). 🌐
71. **hacker_news** — top Hacker News stories. 🌐
72. **github_repo** — stats for a GitHub repo. 🌐
73. **stock_price** — current price of a stock ticker. 🌐
74. **get_crypto_price** — live cryptocurrency price. 🌐
75. **define_word** — dictionary definition of a word. 🌐
76. **synonyms** — synonyms for a word. 🌐
77. **translate_text** — translate text to another language. 🌐
78. **convert_currency** — convert money at live rates. 🌐
79. **convert_units** — length/mass/volume/speed/data/temperature. 💻
80. **time_in** — current local time in a city/timezone. 💻
81. **random_fact** — a random interesting fact. 🌐
82. **this_day** — notable events on this day in history. 🌐

## ✍️ Text & AI utilities
83. **summarize_text** — condense a block of text. 🤖
84. **rewrite_text** — rewrite text in a chosen style/tone. 🤖
85. **fix_grammar** — fix spelling and grammar. 🤖
86. **count_words** — word/character/line counts. 💻
87. **format_json** — pretty-print / validate JSON. 💻
88. **base64_tool** — base64 encode or decode. 💻
89. **hash_text** — md5 / sha1 / sha256 a string. 💻
90. **generate_password** — a strong random password. 💻
91. **qr_generate** — make a QR-code image for a link or text. 💻

## 🎲 Quick utilities & fun
92. **roll_dice** — roll dice (sides, count). 💻
93. **flip_coin** — flip a coin. 💻
94. **random_number** — random number in a range. 💻
95. **days_until** — days until/since a date. 💻
96. **tell_joke** — a random joke. 🌐

## 🚀 Autonomy & self-extension
97. **mission** — autonomous mode: plans, acts, self-verifies with vision, finishes a big goal. 🤖⚠
98. **teach_skill** — Jarvis writes & hot-loads a brand-new tool for itself. ⚠
99. **watch_screen** — proactive mode: periodically glances at your screen and offers help. 🤖📷
100. **open_game** — launch one of the built-in games (breakout, space invaders, tic-tac-toe…). ⚠

---

## Beyond the 100 tools (platform features)
These aren't "tools" — they're how Jarvis works:

- **Three ways in** — voice (wake word "Jarvis"), typed commands in the HUD, and
  Telegram (text **or** voice notes) from your phone.
- **Conversational follow-ups** — after a reply you can keep talking for ~10s
  without repeating "Jarvis".
- **ESC to interrupt** any task mid-action.
- **The live HUD** — cinematic reactor that reacts to your voice, transcript,
  streaming action feed, active brain/key, Gemini daily-quota meter, system
  vitals, plan checklist, reminder countdowns, memory drawer, and a live
  "JARVIS SEES" webcam view.
- **Visual presence** — greets you when you sit down, notices when you leave,
  gives a "while you were away" recap, optional pause-media / lock-PC on leave.
- **Push to phone** — reminders, finished scheduled tasks and alerts arrive on
  Telegram.
- **Fully cloud brain** — Google Gemini with multi-key + multi-model rotation;
  nothing runs on your GPU. Quota usage persists across restarts.
- **Auto-recall** — relevant memories are surfaced to the brain before every command.

## Free-tier notes
- 🤖 tools spend one Gemini request each (free tier ≈ 20/day per key per model;
  rotation across keys + flash/flash-lite stretches it). 🌐-only and 💻 tools
  cost nothing.
- ⚠ tools change your machine's state (files, processes, apps, input) — Jarvis
  uses them deliberately; `delete_path` is reversible (trash folder) by design.
