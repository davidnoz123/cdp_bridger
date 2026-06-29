# CDP Bridger POC Demo Guide

> Markdown extraction from `demo_guide_linked_row_bookmarks.docx`.
> Drawings/images have been intentionally ignored.

## Section overview

| Order | Section title | Brief description |
|---:|---|---|
| <a id="overview-row-01"></a>1 | [[link1] What this demo shows](#section-01-what-this-demo-shows) | A short plain-English overview: the cloud page sends a constrained capture request to a local helper, which captures text from an already-open browser page via local CDP and posts the result back. |
| <a id="overview-row-02"></a>2 | [[link2] Why this matters](#section-02-why-this-matters) | Explain the key idea: the cloud does not get cookies, passwords, browser profile files, or raw CDP access. The local helper enforces policy. |
| <a id="overview-row-03"></a>3 | [[link3] Architecture at a glance](#section-03-architecture-at-a-glance) | Diagram and timeline showing: Cloud UI → Cloud Server → SSE → Local Helper → Chrome CDP → Target Page → POST result back to Cloud Server. |
| <a id="overview-row-04"></a>4 | [[link4] Demo components](#section-04-demo-components) | Table of files and roles: main.py, cloud_server.py, target_server.py, local_helper.py, cdp_tools.py, multi_command_pane_runner.py. |
| <a id="overview-row-05"></a>5 | [[link5] Prerequisites](#section-05-prerequisites) | List required software: Python 3, Chrome/Chromium/Edge, terminal/command prompt, demo source files, local loopback access, and available ports. |
| <a id="overview-row-06"></a>6 | [[link6] Installing Python on Windows](#section-06-installing-python-on-windows) | Step-by-step with screenshots: open Microsoft Store, install Python Install Manager, install Python 3, and verify with python --version. |
| <a id="overview-row-07"></a>7 | [[link7] Python on macOS / Linux / WSL](#section-07-python-on-macos-linux-wsl) | Explain that macOS/Linux may already have python3; show python3 --version; include install hints if missing. |
| <a id="overview-row-08"></a>8 | [[link8] How this proof of concept will be deployed](#section-08-how-this-proof-of-concept-will-be-deployed) | Explains that the POC is shared as a .zip and walked through on a setup call because deployment friction is the problem the product is designed to reduce. |
| <a id="overview-row-09"></a>9 | [[link9] How to run the demo](#section-09-how-to-run-the-demo) | Unzip the source-code folder, confirm Python and Chrome are available, open a terminal, and run python main.py or python3 main.py. |
| <a id="overview-row-10"></a>10 | [[link10] What should happen when it starts](#section-10-what-should-happen-when-it-starts) | Show expected terminal panes and log messages: cloud server, target server, local helper, CDP browser ready, SSE connected. |
| <a id="overview-row-11"></a>11 | [[link11] The cloud page](#section-11-the-cloud-page) | Screenshot and explanation of http://127.0.0.1:8001/: capture button, dropdown, helper status, latest capture, raw JSON results. |
| <a id="overview-row-12"></a>12 | [[link12] The target website](#section-12-the-target-website) | Screenshots of http://127.0.0.1:8002/, /login, and /account; explain the demo login cookie and textarea. |
| <a id="overview-row-13"></a>13 | [[link13] Running a capture job](#section-13-running-a-capture-job) | Step-by-step: select prefix, click capture, job created, helper receives SSE job, helper captures target page, result appears. |
| <a id="overview-row-14"></a>14 | [[link14] Understanding the result](#section-14-understanding-the-result) | Explain friendly latest capture table: Received, Job, Status, Captured URL, Title. Then explain raw JSON fields. |
| <a id="overview-row-15"></a>15 | [[link15] Security and trust boundary](#section-15-security-and-trust-boundary) | Dedicated explanation: cloud requests; helper decides; helper allows only configured prefixes; no raw CDP commands; no cookie/profile reading. |
| <a id="overview-row-16"></a>16 | [[link16] Suggested screenshots checklist](#section-16-suggested-screenshots-checklist) | A checklist of screenshots to capture for the guide: Python install, terminal panes, cloud UI, target page, successful result, failure result. |
| <a id="overview-row-17"></a>17 | [[link17] Limitations of this POC](#section-17-limitations-of-this-poc) | Be honest: local-only, simple HTTP server, no authentication, no production SSE infrastructure, simplistic tab-selection policy. |
| <a id="overview-row-18"></a>18 | [[link18] Next steps / production direction](#section-18-next-steps-production-direction) | Explain possible evolution: signed helper, user account, explicit permissions, richer capture types, packaged installer, real cloud deployment. |

<a id="section-01-what-this-demo-shows"></a>

## [[back]](#overview-row-01) What this demo shows

This demo shows a proof-of-concept software deployment framework in which a user’s web account can coordinate work with a trusted local Python bridge running on the user’s own computer. The web account can create high-level jobs, and the local bridge can carry out those jobs using local capabilities such as browser automation, file access, or local scripts, subject to policy checks in the bridge.

In this demonstration, the cloud page sends a constrained capture request to the local bridge. The bridge then uses Chrome DevTools Protocol (CDP) on the user’s machine to read visible text from a browser page that the user is already logged into. The captured result is posted back to the cloud page and displayed there.

The important point is that the cloud side is not directly given browser cookies, passwords, raw browser profile files, or unrestricted CDP access. The local bridge remains the controlled execution point. A future version of this pattern could support tools such as scraping a user’s ChatGPT, Claude, or Gemini sessions and uploading them into a searchable “Googlish” archive, while keeping the sensitive browser/session access local to the user’s machine.

<a id="section-02-why-this-matters"></a>

## [[back]](#overview-row-02) Why this matters

This pattern could become a deployment model for trusted local tools. Instead of installing a separate desktop application for every product feature, the user installs and runs one local Python bridge. That bridge becomes the trusted local execution point on the user’s computer.

The user’s web account then provides the product layer around that bridge: the interface, job orchestration, storage, search, billing, permissions, audit history, and feature configuration. In other words, the website becomes the control plane, while the Python bridge becomes the local worker.

This matters because many valuable tasks require access to things that are difficult or impossible for a normal cloud service to reach directly: local files, desktop applications, browser tabs, logged-in web sessions, legacy software, scanners, Office documents, or private data stored on the user’s machine. The bridge can access those things locally, while the web account gives the user a convenient place to start jobs, review results, search captured material, and manage what tools are allowed to run.

This also creates a practical route for small software providers to offer integrations that are normally reserved for much larger platforms. Large companies can integrate across apps, websites, accounts, and devices because they control major software platforms, have privileged APIs, and already occupy a trusted position with the user. A local bridge offers a different route. It creates a user-authorised integration surface across local files, desktop applications, browser tabs, and logged-in web sessions, without requiring the cloud service to own the browser, the operating system, or the third-party platforms being accessed.

The sensitive access happens locally, under the control of the bridge. The cloud account provides the product experience around it: the user interface, configuration, permissions, storage, search, billing, and audit trail.

In this demo, the tool is deliberately simple: the cloud page asks the local bridge to capture visible text from an allowed browser page. But the same deployment pattern could support many other tools, such as archiving ChatGPT, Claude, or Gemini sessions; processing local Word documents; extracting data from legacy applications; or building searchable personal archives.

The key idea is simple: one trusted local bridge, many cloud-managed tools.

<a id="section-03-architecture-at-a-glance"></a>

<a id="architecture-at-a-glance"></a>

## [[back]](#overview-row-03) Architecture at a glance

```mermaid
flowchart TD
    subgraph RemoteServer["Internet"]
        CloudServer["Our Remote Server<br/>Port 8001<br/>cloud_server.py"]
    end

    subgraph UserComputer["User's Computer"]
        Helper["Local Python Bridge<br/>local_helper.py"]

        subgraph BrowserEnv["Browser Environment"]
            CloudUI["Our Remote Server<br/>Browser tab"]
            Chrome["Chrome CDP<br/>127.0.0.1:9222"]
            PrivatePage["User's Private Account<br/>Browser tab"]
        end
    end

    subgraph InternetServers["Internet"]
        TargetSites["127.0.0.1:8002/account (target_server.py)<br/>OR<br/>chatgpt.com<br/>claude.ai<br/>gemini.google.com<br/>etc."]
    end

    CloudUI -->|"Create capture job"| CloudServer
    CloudServer -->|"SSE job*"| Helper
    Helper -->|"CDP read*"| Chrome
    Chrome -->|"Read visible page"| PrivatePage
    TargetSites -->|"Serves page/app content"| PrivatePage
    Helper -->|"POST result with job_id"| CloudServer
    CloudServer -->|"Show latest capture"| CloudUI
```

<a id="architecture-link-sse-job"></a>
[[link]](#footnote-sse-job) SSE job*

<a id="architecture-link-cdp-read"></a>
[[link]](#footnote-cdp-read) CDP read*

### Timeline: how the demo works

1. The user opens **Our Remote Server — Browser tab** on the user’s computer.
2. The user clicks **Create capture job**.
3. **Our Remote Server — Browser tab** sends **Create capture job** to **Our Remote Server — `cloud_server.py`**.
4. **Our Remote Server — `cloud_server.py`** sends an **SSE job*** to **Local Python Bridge — `local_helper.py`**.
5. **Local Python Bridge — `local_helper.py`** checks the job against its local policy.
6. **Local Python Bridge — `local_helper.py`** sends a **CDP read*** request to **Chrome CDP — `127.0.0.1:9222`**.
7. **Chrome CDP — `127.0.0.1:9222`** performs **Read visible page** against **User’s Private Account — Browser tab**.
8. **Internet** servers serve the private account page content, for example `127.0.0.1:8002/account (target_server.py)`, `chatgpt.com`, `claude.ai`, `gemini.google.com`, etc.
9. **Local Python Bridge — `local_helper.py`** sends **POST result with `job_id`** to **Our Remote Server — `cloud_server.py`**.
10. **Our Remote Server — `cloud_server.py`** updates **Our Remote Server — Browser tab** to **Show latest capture**.


<a id="section-04-demo-components"></a>

## [[back]](#overview-row-04) Demo components

This proof of concept is deliberately split into a small number of simple Python files. Each file has a clear role, so the moving parts can be understood separately.

| File                           | Role                                                                                                                                                                                                                                                                                       |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `main.py`                      | Starts the demo. It launches the required local services, starts Chrome with CDP enabled, and opens the terminal panes so the user can see what is happening.                                                                                                                              |
| `cloud_server.py`              | Provides the demo “cloud” web interface and job server. In the POC it runs locally on port `8001`, but conceptually it represents the user’s web account / remote server. It serves the browser UI, creates jobs, streams jobs to the local helper using SSE, and receives posted results. |
| `target_server.py`             | Provides a fake target website for safe testing. In the POC it runs locally on port `8002` and simulates a logged-in private account page that the helper can capture from.                                                                                                                |
| `local_helper.py`              | The local Python bridge. It connects to the server, waits for jobs, checks whether a requested capture is allowed, talks to Chrome through CDP, reads limited page content, and posts the result back.                                                                                     |
| `cdp_tools.py`                 | Contains the lower-level Chrome/CDP helper code. It is responsible for launching or finding Chrome with a debugging port and sending simple CDP commands.                                                                                                                                  |
| `multi_command_pane_runner.py` | Runs multiple commands in one terminal-style view so the demo can show the cloud server, target server, helper, and other processes at the same time.                                                                                                                                      |

The important separation is between the **server-side orchestration** and the **local execution point**. The server can create high-level jobs, but the local helper decides what it will actually do. The helper is where the local policy checks live.

In this POC, the demo server and target server both run on `127.0.0.1` to keep everything self-contained and easy to inspect. In a production version, `cloud_server.py` would be replaced by a real hosted web service, while the local helper would still run on the user’s own computer.

<a id="section-05-prerequisites"></a>

## [[back]](#overview-row-05) Prerequisites

Before running the demo, the machine needs a few basic pieces of software.

| Requirement                         | Why it is needed                                                                                                               |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Python 3                            | The demo is written in Python. The servers, helper, CDP tooling, and process runner are all Python scripts.                    |
| Chrome, Chromium, or Edge           | The demo uses Chrome DevTools Protocol, so it needs a Chromium-based browser that can be started with a remote debugging port. |
| Terminal / command prompt           | The demo is started from a terminal so the user can see the server, helper, and browser automation logs.                       |
| Demo source files                   | The Python files must be in the same project folder so `main.py` can start the pieces correctly.                               |
| Local network access to `127.0.0.1` | The POC uses local ports such as `8001`, `8002`, and `9222`. These are loopback addresses on the user’s own machine.           |
| Ports available                     | Ports `8001`, `8002`, and `9222` should not already be occupied by stale demo processes or another application.                |

For the easiest first run, use a normal desktop operating system such as Windows, macOS, or Linux with Chrome installed. The demo is easiest to understand when the browser window and the terminal panes are visible side by side.

The POC does not require a real cloud account, a public web server, or real credentials for ChatGPT, Claude, or Gemini. The included `target_server.py` provides a safe local target page so the capture workflow can be tested without touching a real private account.

<a id="section-06-installing-python-on-windows"></a>


## [[back]](#overview-row-06) Installing Python on Windows

For this demo, the easiest way to install Python on Windows is through the **Microsoft Store**. This is a simple approach for non-technical users because the Store handles the download and installation for you.

### Step 1 — Open the Microsoft Store

Open the **Microsoft Store** from the Start menu or taskbar.

![Microsoft Store icon](./python_windows_store_icon.png)

If you do not already see it pinned, open the Start menu and search for **Microsoft Store**.

### Step 2 — Search for Python

In the Microsoft Store search box, search for **Python**.

For this guide, use **Python Install Manager** published by the **Python Software Foundation**.

![Python Install Manager in Microsoft Store](./python_windows_store_page.png)

This Store entry provides the official Python installation experience for Windows.

### Step 3 — Open or install Python Install Manager

- If the page shows an **Install** button, click **Install**.
- If the page shows an **Open** button, Python Install Manager is already installed, and you can click **Open**.

### Step 4 — Install a Python 3 version

Once Python Install Manager opens, choose a current **Python 3** release and install it.

If more than one option is shown, choose a normal current Python 3 version rather than an experimental or specialised build.

### Step 5 — Open Command Prompt or PowerShell

After installation completes, open a new **Command Prompt** or **PowerShell** window.

### Step 6 — Check that Python is installed

Run:

```powershell
python --version
```

You should see a Python 3 version number, for example:

```text
Python 3.12.x
```


<a id="section-07-python-on-macos-linux-wsl"></a>

## [[back]](#overview-row-07) Python on macOS / Linux / WSL

On macOS, Linux, or WSL, Python may already be installed. The command is often `python3` rather than `python`.

Check the installed version:

```bash
python3 --version
```

If Python is installed, you should see a Python 3 version number.

For this demo, no extra Python packages need to be installed. The demo uses Python’s built-in standard library.

If Python is missing, install it using the normal package manager for the system.

On macOS, Python can be installed using the official Python installer or Homebrew:

```bash
brew install python
```

On Debian, Ubuntu, or WSL Ubuntu:

```bash
sudo apt update
sudo apt install python3
```

On Fedora:

```bash
sudo dnf install python3
```

On Arch Linux:

```bash
sudo pacman -S python
```

On macOS and Linux, the browser executable may be called `google-chrome`, `chromium`, `chromium-browser`, or it may live inside the standard Applications folder. The CDP launcher code may need to know where the browser is installed if it cannot find it automatically.

For this demo, the key thing is that Python can run the scripts, and Chrome or another Chromium-based browser can be started with a local debugging port.

<a id="section-08-how-this-proof-of-concept-will-be-deployed"></a>

## [[back]](#overview-row-08) How this proof of concept will be deployed

For this proof of concept, the demo will be shared as a `.zip` file containing the source code and this guide.

I will take you through the deployment on a Google Meet, Zoom, or Telegram call. The purpose of that call is not just to get the demo running. It is also to show, very directly, where the friction is in ordinary software deployment.

Deploying software is often one of the biggest sources of hassle in the software business. A user may need to download files, unzip folders, install dependencies, find the right version of Python, deal with browser permissions, run commands in a terminal, understand error messages, and work out whether a failure is caused by their machine, their network, the software, or the instructions.

That friction is not incidental to this project. It is exactly the problem that the product behind this demo is trying to address.

The proof of concept is deliberately visible and inspectable. You can see the Python files, read the local bridge code, and understand the trust boundary. But the intended product direction is different: the user should not have to manage lots of separate software deployments by hand. Instead, the user would run one trusted local bridge, and their web account would manage the tools, permissions, jobs, results, updates, storage, and product experience around that bridge.

In other words, this demo is distributed as source code so the idea can be inspected. The product behind the demo is about making this kind of deployment smoother, safer, and less repetitive.

For this reason, the guided setup call is part of the demonstration. It shows both the current proof of concept and the larger product opportunity: reducing the friction between a useful cloud-managed tool and the local computer where the work actually needs to happen.

<a id="section-18-next-steps-production-direction"></a>

## [[back]](#overview-row-18) Next steps / production direction

The demo will be run from the unzipped source-code folder during the guided setup call.

At a high level, the process is:

1. Unzip the demo folder.
2. Confirm that Python 3 is available.
3. Confirm that Chrome, Chromium, or Edge is available.
4. Open a terminal in the demo folder.
5. Run the demo startup script:

```powershell
python main.py
```

On macOS, Linux, or WSL, the command may be:

```bash
python3 main.py
```

The startup script launches the local demo components and opens the browser pages used in the walkthrough.

The exact command may vary slightly depending on the operating system and how Python is installed. During the setup call, I will check this with you and help resolve any local machine issues that appear.


<a id="section-09-what-should-happen-when-it-starts"></a>

## [[back]](#overview-row-09) What should happen when it starts

Placeholder: Draft content for “What should happen when it starts” goes here. Planning note: Show expected terminal panes and log messages: cloud server, target server, local helper, CDP browser ready, SSE connected.

<a id="section-10-the-cloud-page"></a>

## [[back]](#overview-row-10) The cloud page

Placeholder: Draft content for “The cloud page” goes here. Planning note: Screenshot and explanation of http://127.0.0.1:8001/: capture button, dropdown, helper status, latest capture, raw JSON results.

<a id="section-11-the-target-website"></a>

## [[back]](#overview-row-11) The target website

Placeholder: Draft content for “The target website” goes here. Planning note: Screenshots of http://127.0.0.1:8002/, /login, and /account; explain the demo login cookie and textarea.

<a id="section-12-running-a-capture-job"></a>

## [[back]](#overview-row-12) Running a capture job

Placeholder: Draft content for “Running a capture job” goes here. Planning note: Step-by-step: select prefix, click capture, job created, helper receives SSE job, helper captures target page, result appears.

<a id="section-13-understanding-the-result"></a>

## [[back]](#overview-row-13) Understanding the result

Placeholder: Draft content for “Understanding the result” goes here. Planning note: Explain friendly latest capture table: Received, Job, Status, Captured URL, Title. Then explain raw JSON fields.

<a id="section-14-security-and-trust-boundary"></a>

## [[back]](#overview-row-14) Security and trust boundary

Placeholder: Draft content for “Security and trust boundary” goes here. Planning note: Dedicated explanation: cloud requests; helper decides; helper allows only configured prefixes; no raw CDP commands; no cookie/profile reading.

<a id="section-15-suggested-screenshots-checklist"></a>

## [[back]](#overview-row-15) Suggested screenshots checklist

Placeholder: Draft content for “Suggested screenshots checklist” goes here. Planning note: A checklist of screenshots to capture for the guide: Python install, terminal panes, cloud UI, target page, successful result, failure result.

<a id="section-16-limitations-of-this-poc"></a>

## [[back]](#overview-row-16) Limitations of this POC

Placeholder: Draft content for “Limitations of this POC” goes here. Planning note: Be honest: local-only, simple HTTP server, no authentication, no production SSE infrastructure, simplistic tab-selection policy.

<a id="section-17-next-steps-production-direction"></a>

## [[back]](#overview-row-17) Next steps / production direction

Placeholder: Draft content for “Next steps / production direction” goes here. Planning note: Explain possible evolution: signed helper, user account, explicit permissions, richer capture types, packaged installer, real cloud deployment.

## Footnotes

<a id="footnote-sse-job"></a>


### [[back]](#architecture-link-sse-job) SSE job*

SSE means **Server-Sent Events**. It is a simple web mechanism where the local Python bridge opens a long-lived HTTP connection to the server, and the server can then stream small messages down that connection. In this demo, the local bridge connects to the remote server and waits. When the user clicks the capture button in the browser UI, the server writes a small job message onto the SSE stream. The bridge receives that job and decides locally whether it is allowed to act on it.

SSE is one-way: server to client. That is enough here because the bridge only needs to receive job instructions from the server. When the bridge has finished the job, it sends the result back using a normal HTTP `POST`.

The main alternatives would be polling, WebSockets, or a full message queue. Polling would mean the bridge repeatedly asks “is there a job yet?”, which is simple but wasteful and slower. WebSockets allow two-way communication and are more powerful, but they add complexity that this demo does not need. A message queue would be suitable for a larger production system, but it would make the proof of concept harder to understand. SSE was chosen because it is simple, browser/server friendly, easy to debug, and a good fit for “server sends occasional jobs to a waiting local helper”.

<a id="footnote-cdp-read"></a>

### [[back]](#architecture-link-cdp-read) CDP read*

CDP means **Chrome DevTools Protocol**. It is the protocol that developer tools use to inspect and control Chrome. When Chrome is started with a remote debugging port, a local program can connect to that port and ask Chrome about open tabs, page content, the DOM, JavaScript execution, network activity, and other browser state.

In this demo, the local Python bridge uses CDP to read from a browser tab that is already open on the user’s computer. The important word is **read**. The bridge is not taking control of the user’s account, stealing cookies, reading browser profile files, or sending arbitrary browser commands from the cloud. The cloud server sends only a high-level request. The bridge then checks its local policy, selects an allowed tab, and performs a limited read operation against that tab.

This distinction matters because the sensitive access stays local. The remote server does not get direct CDP access. It does not get the user’s browser session. It only receives the final result that the local bridge chooses to send back after applying its own rules. In the proof of concept, the read operation captures visible page text and textarea values. In a production version, this read capability would need to be carefully permissioned, audited, and limited to user-approved sites and data types.
