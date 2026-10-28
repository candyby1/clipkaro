# ClipKaro — Video Downloader & Uploader (bina watermark)

Aik website jahan login kar ke video ka link paste kar ke **bina watermark** download kar sakte ho,
poori Facebook Page / YouTube playlist ki videos **grab** kar ke **bulk download** kar sakte ho,
video ke **clips** kaat sakte ho, **auto captions** bana sakte ho, aur apne **Facebook Page par
seedha video upload** kar sakte ho.

## Kya kya hai

- 👤 **Login system** — Sign Up / Login (SQLite, password hash ho kar save hota hai, plain text kabhi nahi).
  Download, Grabber, Clipper, Subtitles — sab ke liye login zaroori hai. Har user ko sirf **apni** jobs/history dikhti hai.
- 📥 **Download tab** — Facebook, YouTube, TikTok, Instagram, X waghera se single video download
  (original file, koi watermark nahi lagta). Hamesha **best available quality** — aur download ke baad
  asal quality (jaise 720p / 1080p) file ko check kar ke sach mein dikhayi jati hai.
  "4K" sirf tab likha jata hai jab asal video 2160p ya zyada ho.
- 🔗 **Link Grabber tab** — Facebook Page/profile ya YouTube playlist/channel ka link do, saari videos ki
  list (title, duration, thumbnail) aa jayegi. Checkbox se chun kar **Bulk Download** — videos ek ek kar ke
  server par download hoti hain, live progress aur har file ka alag download link milta hai.
  Saari jobs **My Jobs** tab mein history ki surat mein rehti hain.
- 🍪 **Facebook cookies** — Facebook aksar Page ki list ke liye login maangta hai. Grabber tab mein apni
  **cookies.txt** (Netscape format) upload kar do; file sirf server par rehti hai aur sirf tumhare
  Grab/Download mein use hoti hai.
- ✂️ **Clipper tab** — video upload karo (ya bulk download ki hui file select karo), start/end seconds do,
  exact clip ban kar download ho jayega.
- 💬 **Subtitles tab** —
  - **Auto Captions:** video ki awaz se AI (faster-whisper) `.srt` captions banata hai.
  - **Burn Subtitles:** apni `.srt` file kisi video ke andar pakki burn kar do.
- 📤 **Upload tab** — Facebook Login se apna Page select karo, video upload karo — seedha Page par.
  Is mein **Caption Template** bhi hai: `{title}` wala template aur hashtags aik dafa set karo,
  har video par khud lag jayenge. (Ye Facebook login browser mein hota hai — is ke liye neeche wali
  App ID setup chahiye. ClipKaro account login is se alag hai.)

## Apne PC par chalana (testing)

```bash
cd video-tool
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

Phir browser mein kholo: http://127.0.0.1:5000 — pehle **Sign Up** kar ke account banao, phir sab features khul jayenge.

**ffmpeg zaroori hai** (clipper, subtitles burn aur video merge ke liye) — system par install hona chahiye.

Data (accounts, jobs, cookies) `data/` folder mein save hota hai. Chaho to `CLIPKARO_DATA`
env var se doosri jagah rakh sakte ho. Auto captions ka model `CLIPKARO_WHISPER_MODEL`
se badal sakte ho (default: `base`; `small` zyada accurate lekin slow hai).

## Website online karna (taake sab log use karein) — Render.com (free)

1. Is folder ko GitHub par push karo (aik repository banao).
2. [render.com](https://render.com) par free account banao → **New +** → **Web Service** → apni repository select karo.
3. **Runtime** mein **Docker** chuno — Dockerfile pehle se folder mein hai, is mein **ffmpeg** bhi included hai. (Bina ffmpeg ke video download fail ho jayega, is liye normal Python runtime **na** chunna.) Plan: **Free**.
4. Deploy dabao — 3-4 minute mein tumhari website ka link mil jayega (jaise `clipkaro.onrender.com`).

> Note: free plan par pehli request mein 30-50 second lag sakte hain (server so raha hota hai). Ye normal hai.
> Note 2: Render ke free plan par disk restart par saaf ho jati hai — accounts/jobs wahan permanent nahi rahenge.

## Upload tab ke liye Facebook App ID (zaroori, free, 10 minute)

Upload tab ke liye Facebook ko batana parta hai ke ye tumhari app hai:

1. [developers.facebook.com](https://developers.facebook.com) kholo → **Create App** → **Other** → **Business** → naam do (jaise "ClipKaro") → Create.
2. Dashboard mein **Add Product** → **Facebook Login** → **Set Up**.
3. **Settings → Basic** mein se **App ID** copy karo.
4. `static/index.html` kholo, sab se upar `CONFIG` mein paste karo:
   ```js
   const CONFIG = { FB_APP_ID: "1234567890123456" };
   ```
5. Facebook Login → Settings mein **Valid OAuth Redirect URIs** mein apni website ka link add karo (jaise `https://clipkaro.onrender.com/`).
6. **App Review** — jab tak app "Development mode" mein hai, sirf tum (aur jinhein tum tester banao) upload use kar sakte ho. Sab ke liye kholne ke liye **App Review** mein `pages_manage_posts` permission ki request karni hogi (Facebook 2-3 din mein approve karta hai).

## Zaroori baatein / limitations

- Kuch websites (khas tor par **YouTube**) data-center IPs se direct download par bot-check lagati hain —
  aisi soorat mein error saaf dikh jata hai; cookies wala option madad kar sakta hai.
- **Facebook Page** ki video list ke liye aksar **login cookies** zaroori hoti hain — bina cookies ke
  kuch Pages ki list nahi milti aur tool error mein yehi wajah bata deta hai.
- Auto captions ke liye `faster-whisper` chahiye. Agar kisi server par wo install/load na ho sake to
  baqi sab features chalte rahenge — sirf Auto Captions off rahega, `.srt` Burn phir bhi chalega.
- Sirf **public** videos download hoti hain.
- Kisi aur ki video download kar ke apne naam se dobara upload karna unki ijazat ke baghair ghalat hai — aur Facebook/YouTube par copyright strike lag sakti hai.
