# Microgrid Optimizer — how to run it (no coding)

The tool is a small web page (`app.py`). Your team **never opens Python or notebooks**.
They edit the Google Sheet, open the web page, and click **Run optimization**.

There are two ways to host it. Start with A to try it, move to B to give the whole company a link.

---

## A. Quick pilot — run on one computer (5 min)

On a computer that has the project folder:

1. Install once (one command):
   ```
   pip install -r requirements.txt
   ```
2. Start the app:
   ```
   streamlit run app.py
   ```
3. A browser tab opens at `http://localhost:8501`. Anyone on the **same office Wi‑Fi** can use it
   via the "Network URL" Streamlit prints (e.g. `http://192.168.x.x:8501`).

The app stays up only while that computer runs the command. Good for testing, not for "always on".

---

## B. Company link — free hosting (Streamlit Community Cloud)

This gives everyone a permanent URL, always on, free.

1. Make sure this project is in a **GitHub repository** (it already is for you).
2. Go to **https://share.streamlit.io** and sign in with GitHub.
3. **New app** → pick this repository → set **Main file path** to `app.py` → **Deploy**.
4. After ~2 minutes you get a URL like `https://your-company-microgrid.streamlit.app`.
   Share that link with your team. Done.

**Keep it private (internal only):** in the app's **Settings → Sharing**, turn on
"Only specific people can view this app" and add your colleagues' Google emails. They'll sign in
with Google to open it.

When you change the code, push to GitHub and the app updates itself.

---

## How your team uses it (every time)

1. Open the **input Google Sheet** (link is in the app's sidebar) and edit the `setup` tab —
   choose components, set grid price, load profile, etc.
2. Open the **app URL** and click **▶ Run optimization**.
3. Read the results; download the Bill of Materials / dispatch as CSV if needed.

> The sheet must be shared as **Anyone with the link → Viewer** so the app can read it.
> Each click re-reads the latest sheet, so just edit and re-run to see new results.

---

## If something looks wrong
- **"Could not run … not publicly readable"** → the Google Sheet isn't link-shared as Viewer.
- **Solver status not optimal** → usually an off-grid setup (`grid_max_fraction = 0`) that can't
  meet the load, or a missing component. Adjust the `setup` tab and run again.
