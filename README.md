# mac-scaleout

메인 맥이 다른 맥(워커)에 **Claude Code 일을 나눠 맡기고 결과만 받아 오는** 작은 도구예요.
메인 메모리 · CPU가 모자라거나, 오래 걸리는 빌드 · 테스트 · 조사를 옆 맥에서 돌리고 싶을 때 써요.

```
메인 맥 ── scaleout run ──▶ 워커 대기열(~/.scaleout/queue)
   ▲                           │ 워커 로그인 세션의 실행기(LaunchAgent)가 집어 감
   │                           ▼
   └── scaleout wait ◀── 워커의 Claude Code(claude -p) 최종 보고
```

- **어느 망에 있어도 붙어요.** 워커가 메인의 Cloudflare SSH 터널로 역방향 터널을 걸어 둬요(메인 `localhost:22xx` → 워커 SSH). 같은 와이파이면 직접도 붙어요.
- **워커 로그인을 그대로 써요.** 토큰을 따로 만들지 않아요(아래 「왜 이렇게 만들었나」).
- **macOS 기본만으로 돌아요.** bash 3.2 · ssh · launchd. 터널을 쓰면 워커에 `cloudflared`(설치기가 Homebrew로 깔아요).

## 준비물

| 어디 | 무엇 |
|---|---|
| 메인 | 원격 로그인 켜짐 · (밖에서도 붙이려면) `ssh://localhost:22`로 가는 Cloudflare Tunnel 호스트 |
| 워커 | Claude Code 설치 · 로그인, 전원 연결 시 잠자기 방지. 원격 로그인은 꺼져 있어도 돼요(초대 방식이 켜 줘요) |

## 설치

### 1. 메인에서 한 번
```bash
gh repo clone yuna-studio/mac-scaleout && ./mac-scaleout/install.sh
```
- `~/.local/share/scaleout`에 복사하고 `~/.local/bin/scaleout`을 만들어요.
- Cloudflare 설정(`~/.cloudflared/config.yml`)에서 SSH 호스트를 찾아요. 다른 호스트면 `./install.sh --main-host <호스트>`로 넘겨요.
- Claude 서브에이전트 `scaleout`(`~/.claude/agents/scaleout.md`)도 깔아요.

### 2. 워커 붙이기 (둘 중 하나)

**A. 메인에서만 (워커 원격 로그인이 켜져 있고 같은 망일 때)**
```bash
scaleout add macbook kong@192.168.0.10 --dir ~/workspace
```
워커 비밀번호를 한 번 묻고, 나머지는 메인이 SSH로 다 해요. 실행기 · 터널 · 키 교환 · 시험 일까지예요.

**B. 초대 한 줄 (워커가 다른 망이거나 원격 로그인이 꺼져 있을 때)**
```bash
scaleout invite macbook --dir ~/workspace     # 메인 — 한 줄짜리 명령이 나와요
```
나온 한 줄(`bash -c "$(echo … | base64 -D)"`)을 **워커 터미널에 붙여 넣으면** 끝이에요. 원격 데스크톱으로 넣어도 돼요.
- 원격 로그인이 꺼져 있으면 켜요(그때 워커 비밀번호를 한 번 물어요).
- 메인에서 패키지를 받아 설치하고, 자기 키를 메인에 등록하고, 메인 키를 받아 넣어요.
- 초대 키는 24시간 안에 **한 번만** 써지고, 메인에서 `scaleout _enroll`만 돌게 묶여 있어요.

## 쓰는 법

```bash
scaleout status                         # 워커마다 붙는지 · 남은 메모리 · 도는 일
id=$(echo "레포 받아서 테스트 돌리고 결과만 알려줘" | scaleout run -w auto -d ~/workspace)
scaleout wait "$id"                     # 끝나면 워커 Claude의 최종 보고 (540초마다 끊어서, 아직이면 종료 코드 2)
scaleout jobs | logs <id> | cancel <id> | result <id>
scaleout exec macbook -- df -h          # 워커에서 셸 명령
scaleout doctor macbook                 # 워커 사양 · 도구 · 실행기 · 터널 상태
scaleout remove macbook                 # 워커 떼기 (양쪽 설정 정리)
```
- `-w auto`는 남은 메모리가 많고 도는 일이 적은 워커를 골라요.
- Claude Code 안에서는 서브에이전트로 맡겨요: 「scaleout 서브에이전트로 이 빌드를 워커에서 돌려 줘」. 지시에는 워커에서 할 일 · 폴더 · 결과를 남길 곳(예: 브랜치 푸시)을 다 적어요. 워커 Claude는 메인 대화를 몰라요.
- 종료 코드: 0 성공 · 1 워커 Claude 실패 · 2 아직 도는 중 · 3 워커에 못 붙음 · 4 사용법 · 5 없음

## 워커가 무인으로 할 수 있는 것

`~/.scaleout/settings.json`(처음 설치 때만 깔고, 고치면 그대로 둬요)
- **허용:** 파일 읽기 · 쓰기, git, `gh pr create/view`, npm · node · flutter · xcodebuild 같은 빌드 도구, 읽기 명령
- **막음:** 강제 푸시, main · master · dev · release 푸시, `gh pr merge`, 워크플로 실행, 릴리스, `rm -rf`, `sudo`, firebase · wrangler 배포

## 보안

- 메인 → 워커: 전용 키 `~/.ssh/scaleout_ed25519`
- 워커 → 메인: 워커 키는 메인 `authorized_keys`에 `restrict,port-forwarding,permitlisten="localhost:<포트>",command=…`로 들어가요. **터널만 열 수 있고 메인 셸에는 못 들어와요.**
- 초대 키: `restrict,command="scaleout _enroll <이름>"` — 패키지 받기 · 등록만 되고, 쓰면 바로 지워지고, 기한이 지나면 안 먹어요.

## 왜 이렇게 만들었나 (함정들)

- **SSH 세션은 맥 키체인을 못 열어요.** 그래서 SSH로 `claude -p`를 돌리면 로그인돼 있어도 「Not logged in」이 나와요. 일은 워커 **로그인 세션의 LaunchAgent**(`dev.scaleout.runner`, `QueueDirectories`)가 돌려요.
- **명령을 슬랙 · 폰으로 옮겨 붙이면 깨져요.** 줄 앞 공백이 특수 공백(`\240`)으로, 따옴표가 바뀌어요. 그래서 파일은 SSH로 보내고, 초대는 base64 한 줄이에요.
- **절차를 `| bash`로 흘리면 안에서 `ssh`가 남은 절차를 먹어요.** 그래서 `bash -c "…"` 꼴이에요(비밀번호 질문도 이래야 돼요).
- **macOS 기본 bash는 3.2예요.** `wait -n` · 연관 배열이 없고, `$이름` 바로 뒤의 한글을 이름으로 읽기도 해요. 그래서 `${이름}`으로 감싸요.
- **터널을 다시 올리면 그 터널로 들어온 설치가 끊겨요.** 그래서 설정이 그대로면 그대로 두고, 바뀌면 몇 초 뒤 따로 올려요.

## 파일

| 경로 | 무엇 |
|---|---|
| `bin/scaleout` | 메인 CLI |
| `lib/common.sh` | 공용 함수 |
| `worker/setup.sh` | 워커 설치 · 제거 (`--uninstall`) |
| `worker/runner.sh` | 워커 실행기 (동시 `MAX_JOBS`개, 일마다 `JOB_MINUTES` 한도) |
| `worker/settings.json` | 워커 Claude 허용 목록 기본값 |
| `claude/scaleout.md` | Claude 서브에이전트 정의 |
| `tests/smoke.sh` | 시험 (워커 · 네트워크 없이) |

시험: `bash tests/smoke.sh`
