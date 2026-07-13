# Git 常用指令筆記

一份針對「個人專案 + GitHub」情境的 Git 操作速查表。
照著做就能處理 9 成日常工作：上傳新專案、開分支、每次推送、合併回 main。

---

## 名詞快速理解

| 名詞 | 白話解釋 |
|---|---|
| repository（repo / 倉庫） | 一個被 Git 管理的專案資料夾 |
| commit | 一次「存檔」，記錄這次改了什麼 |
| branch（分支） | 一條獨立的開發線，互不干擾 |
| `main` | 主分支，通常放穩定、正式的版本 |
| `origin` | 遠端倉庫的代稱（這裡就是你的 GitHub） |
| push | 把本地 commit 上傳到 GitHub |
| pull | 把 GitHub 上的更新下載到本地 |
| merge | 把一條分支的內容合併進另一條 |

> 流程心法：**改檔案 → `add`（選要存的）→ `commit`（存檔）→ `push`（上傳）**

---

## 0. 第一次使用先設定身分（每台電腦做一次就好）

```bash
git config --global user.name "你的名字"
git config --global user.email "你的email@example.com"
```

---

## 1. 如何將一個新的專案上傳到 GitHub

### 步驟 A：先在 GitHub 網站建立一個空 repo
到 GitHub 點 **New repository**，取好名字，**不要**勾選 "Add a README"（保持全空，避免衝突），建立後複製它的網址。

### 步驟 B：在本地專案資料夾執行

```bash
# 1. 進到你的專案資料夾後，初始化 git
git init

# 2. 把所有檔案加入追蹤
git add .

# 3. 第一次存檔
git commit -m "first commit: 專案初始化"

# 4. 把預設分支命名為 main
git branch -M main

# 5. 綁定遠端 GitHub repo（網址換成你自己的）
git remote add origin https://github.com/你的帳號/你的repo.git

# 6. 上傳，並建立追蹤關係（之後就能直接打 git push）
git push -u origin main
```

> 💡 **小提醒**：上傳前先建立 `.gitignore`，把不該上傳的東西（`.venv/`、`.env`、`__pycache__/`、`logs/` 等）排除掉。
> 例如本專案的 `.env` 含 API 金鑰，**絕對不能**上傳。

`.gitignore` 範例：

```gitignore
.venv/
__pycache__/
*.pyc
.env
logs/
```

---

## 2. 如何建立分支

開分支是為了在不影響 `main` 的情況下開發新功能。

```bash
# 建立並「直接切換」到新分支（最常用）
git checkout -b feat/新功能名稱

# 或用較新的寫法（功能一樣）
git switch -c feat/新功能名稱
```

常見分支命名慣例：

| 前綴 | 用途 | 例子 |
|---|---|---|
| `feat/` | 新功能 | `feat/fubon-api` |
| `fix/`  | 修 bug | `fix/notion-timeout` |
| `refactor/` | 重構 | `refactor/api-worker` |

其他分支操作：

```bash
git branch              # 列出所有本地分支（* 是目前所在）
git checkout main       # 切換回 main
git switch main         # 同上（新寫法）
```

---

## 3. 每次要如何上傳到 GitHub

這是**最高頻**的循環，每寫一段就做一次：

```bash
# 1. 看看改了哪些檔案（紅色=未加入，綠色=已加入）
git status

# 2. 把要存檔的變更加入（. 代表全部）
git add .

# 3. 存檔，並寫清楚這次做了什麼
git commit -m "feat: 接上富邦 snapshot 行情 API"

# 4. 上傳到 GitHub
git push
```

> 💡 第一次推送「新分支」時，要先告訴 GitHub 這條分支：
> ```bash
> git push -u origin feat/新功能名稱
> ```
> 之後在這條分支就只要打 `git push` 即可。

好的 commit 訊息範例：

```text
feat: 新增 email 到價通知
fix: 修正 Notion 更新偶爾逾時的問題
docs: 補上 Git 操作筆記
```

---

## 4. 如何將新的分支合併回 main

功能在分支上開發、測試完成後，合併回 `main`。有兩種做法：

### 做法 A：GitHub 網頁開 Pull Request（推薦，留得下紀錄）

1. 先把分支推上去：`git push -u origin feat/新功能名稱`
2. 打開瀏覽器：`https://github.com/你的帳號/你的repo/compare/main...feat/新功能名稱`
3. 按 **Create pull request** → 再按 **Merge pull request**
   - 想把多個雜亂的 commit 壓成一條乾淨訊息，選 **Squash and merge**

### 做法 B：本地命令列直接合併（快，個人專案適用）

```bash
# 1. 切回 main
git checkout main

# 2. 先把 main 更新到最新（重要！避免基於舊版本合併）
git pull origin main

# 3. 合併你的分支
git merge feat/新功能名稱

# 4. 推上去
git push origin main
```

### 合併完成後，清理分支（選用）

```bash
git branch -d feat/新功能名稱               # 刪本地分支
git push origin --delete feat/新功能名稱    # 刪遠端分支
```

> ⚠️ **踩雷提醒**：如果本地 `main` 跟遠端 `origin/main` 分歧了（`git branch -vv` 顯示「領先 X，落後 Y」），
> 通常是本地 main 過時。確認遠端才是對的版本後，合併前先把本地 main 拉齊：
> ```bash
> git checkout main
> git fetch origin
> git reset --hard origin/main   # ⚠️ 會丟棄本地 main 未推送的變更，確認過再用
> ```

---

## 5. 其他常用救命指令

```bash
# 查看歷史紀錄（精簡單行）
git log --oneline -10

# 查看目前狀態與所在分支
git status -sb

# 還沒 commit，想丟棄某個檔案的修改（回到上次 commit 狀態）
git checkout -- 檔名

# 已經 git add 了，想取消加入（但保留修改內容）
git restore --staged 檔名

# 從 GitHub 下載最新版到本地
git pull

# 把別人/另一台電腦的專案抓下來
git clone https://github.com/你的帳號/你的repo.git
```

---

## 6. 典型一日工作流程（總結）

```bash
git checkout main && git pull          # 開工：先更新 main
git checkout -b feat/today-work        # 開一條新分支來做事

# …… 寫程式 ……

git add .                              # 加入變更
git commit -m "feat: 完成今天的功能"    # 存檔
git push -u origin feat/today-work     # 上傳分支

# 功能完成、測試 OK 後，合併回 main（做法 A 或 B 擇一）
```

---

> 📌 黃金守則
> 1. **commit 訊息寫清楚**，未來的你會感謝現在的你。
> 2. **`.env`、金鑰、密碼絕對不要 push**，用 `.gitignore` 擋掉。
> 3. **合併前先 `pull`**，確保基於最新版本。
> 4. 不確定狀態時，先打 `git status`，它會告訴你現在在哪、發生什麼事。