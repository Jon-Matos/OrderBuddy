const express = require('express');
const path = require('path');
const fs = require('fs');
const multer = require('multer');
const FormData = require('form-data');

// Use the global fetch and FormData provided by Node 18+ (no node-fetch/form-data require)
const app = express();
app.use(express.json());

// ensure uploads directory exists for multer
const uploadsDir = path.join(__dirname, 'uploads');
if (!fs.existsSync(uploadsDir)) fs.mkdirSync(uploadsDir);

// configure multer to store uploaded files in ./uploads
const upload = multer({ dest: uploadsDir });

const TRUSSED_LLM_URL = "https://fauengtrussed.fau.edu/provider/generic/chat/completions"
const TRUSSED_STT_URL = "https://fauengtrussed.fau.edu/provider/generic/audio/transcriptions"
const API_KEY = "2lIEsvI86eeCE8FtS09mnOp505iChe4SOQSwLOnUb0WDpbQ4";

const MENU_FILE_PATH = path.join(__dirname, 'menu.txt');
let menuContent = '';

try {
  // Read the menu file content synchronously
  menuContent = fs.readFileSync(MENU_FILE_PATH, 'utf8');
  // console.log(menuContent)
  console.log('Menu file loaded successfully.');
} catch (error) {
  console.error(`ERROR: Could not read menu file at ${MENU_FILE_PATH}`);
  console.error('Please create a menu.txt file with "item : $price" entries.');
}

const systemSetup  = `
 You are the Virtual Order Assistant. Your primary function is to process and track a customer's order based on the provided menu.

  --- CURRENT MENU ---
  ONLY USE the exact Menu Items and Prices below when processing the order.
  Do not use information about items or prices that are not listed here.
  Menu Items:
  ${menuContent}
  --------------------

  **OUTPUT MANDATE:**
  You MUST respond conversationally after adding an item. Your response must follow this EXACT structure:

  "Sure [Item Name] is [Price] would you like anything else?"

  If the item is not on the menu, respond with:
  "I'm sorry, that item is not on the current menu. Would you like to order something else?"

  DO NOT include any other text, order summaries, or greetings.
    `;

app.use(express.static(path.join(__dirname, '.')));

app.post('/openai/complete', async (req, res) => {
  try {
    const prompt = req.body.prompt;

    const response = await fetch(TRUSSED_LLM_URL, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: 'gpt-4o', 
        messages: [
          { role: 'system', content: systemSetup },
          { role: 'user', content: prompt }
        ],
      }),
    });

    if (!response.ok) {
      console.error("Trussed error:", await response.text());
      return res.status(500).send("Error from Trussed endpoint");
    }

    const data = await response.json();
    res.json({ text: data.choices[0].message.content });
  } catch (error) {
    console.error('Error calling Trussed:', error);
    res.status(500).send('Error processing your request');
  }
});

// === Speech-to-Text Endpoint ===
app.post("/audio/transcriptions", upload.single("audio"), async (req, res) => {
  try {
    const audioPath = req.file.path;

    // Use the `form-data` package so the file stream is sent as a proper upload
    const form = new FormData();
    form.append('file', fs.createReadStream(audioPath));
    form.append('model', 'whisper-1'); // typical STT model name; confirm your Trussed options

    const formHeaders = form.getHeaders();

    const response = await fetch(TRUSSED_STT_URL, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${API_KEY}`,
        ...formHeaders,
      },
      body: form,
    });

    const data = await response.json();
    fs.unlinkSync(audioPath); // clean up uploaded temp file

    if (data.text) {
      res.json({ text: data.text });
    } else {
      console.error("Transcription error:", data);
      res.status(500).send("Error transcribing audio");
    }
  } catch (err) {
    console.error("STT error:", err);
    res.status(500).send("Speech-to-text failed");
  }
});

app.listen(3000, function () {
  console.log('App is listening on port 3000!');
});
