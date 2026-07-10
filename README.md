# ShikhaAI

An AI-powered desktop app that turns any topic into either a narrated, illustrated video or a two-host educational podcast — built for students, teachers, and anyone who learns better by watching or listening than by reading.

Give it a topic and a grade level, and ShikhaAI will:
- Write an age-appropriate script
- Illustrate it with AI-generated scenes and assemble a narrated video, **or**
- Turn it into a lively two-host podcast conversation

---

## Features

**Video mode**
- Auto-generated narration script tailored to the selected grade level
- AI-illustrated scenes with smooth Ken Burns pan/zoom motion (not static slides)
- Natural narration audio with a choice of voices
- **Storyboard view** — see every generated scene as a thumbnail strip; click one to view it full-size, or regenerate just that scene without re-rendering the whole video

**Podcast mode**
- Two AI hosts, Shimmer (energetic) and Echo (thoughtful), with genuinely distinct speaking styles
- Natural back-and-forth dialogue with humor and real-world examples
- Optional ambient background music bed (off by default, adjustable volume)
- Adjustable playback speed (0.75x–2x)

**Library**
- Every generated video and podcast is saved automatically, with its script
- Search by topic, filter by grade level, sort by date or alphabetically
- Persists across restarts

**Settings**
- Three model tiers (Budget / Balanced / Higher quality) to control cost vs. output quality
- Narrator voice picker with an in-app preview
- Adjustable number of illustrated scenes per video
- Podcast background music toggle and volume

**Interface**
- Modern dark UI with a custom icon set and smooth hover animations
- Toast notifications instead of intrusive system popups
- Cross-platform video/audio playback controls (play, pause, resume)

---

## Requirements

- **Python 3.9 or newer**
- **VLC media player** installed on your system (required for in-app playback) — [download here](https://www.videolan.org/vlc/)
- **An OpenAI API key** with billing enabled — [get one here](https://platform.openai.com/api-keys)

---

## Installation

1. **Clone or download this repository**
   ```bash
   git clone https://github.com/YOUR_USERNAME/ShikhaAI.git
   cd ShikhaAI
   ```

2. **Install the required Python packages**
   ```bash
   pip install -r requirements.txt
   ```

3. **Add your OpenAI API key**
   - Copy `.env.example` to a new file named `.env` in the same folder
   - Open `.env` and replace the placeholder with your real key:
     ```
     OPENAI_API_KEY=sk-your-key-here
     ```

4. **Run the app**
   ```bash
   python run.py
   ```

---

## Usage

1. Choose **Video + Script** or **Podcast** mode at the top of the Create page.
2. Enter a topic and select a grade level.
3. Click **Generate** and wait — progress is shown live, and generation can be cancelled at any point.
4. Once finished, the video/podcast plays automatically with full playback controls.
5. Visit the **Library** tab anytime to revisit, search, or replay anything you've made.
6. Open **Settings** to change the model tier, narrator voice, scene count, or podcast music.

---

## Model tiers

| Tier | Best for | Text model | Image model | Voice model |
|---|---|---|---|---|
| Budget | Cheapest, good for testing | gpt-4.1-nano | gpt-image-1-mini (low) | gpt-4o-mini-tts |
| Balanced (default) | Everyday use | gpt-4.1-mini | gpt-image-1-mini (medium) | gpt-4o-mini-tts |
| Higher quality | Sharper writing and images | gpt-5.4-mini | gpt-image-1.5 (medium) | gpt-4o-mini-tts |

Model prices change over time — check [OpenAI's pricing page](https://openai.com/api/pricing/) for current rates. A typical short video on the Balanced tier costs well under $0.50 in API usage.

---

## Project structure

```
run.py                   entry point
shikhaai/
  main.py                 startup and API key check
  app.py                   the application UI
  ai_services.py           OpenAI calls and video assembly
  music.py                 procedural background-music synthesis
  components.py            custom UI components (buttons, toasts, etc.)
  icons.py                 icon loading
  player.py                media playback
  storage.py                history and settings persistence
  config.py                 app-wide configuration and constants
  assets/                    icons and logo
```

---

## Credits

- Icons by [Lucide](https://lucide.dev) (ISC License)
- Powered by the [OpenAI API](https://platform.openai.com/)

## License

This project does not yet specify a license. If you plan to share or accept contributions, consider adding one (e.g. the [MIT License](https://choosealicense.com/licenses/mit/)).
