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


const systemSetup = "You are the Virtual Order Assistant, a friendly and efficient AI assistant for a fast food restaurant. You help customers place orders, answer questions about the menu, and provide information about promotions and deals. Always be polite and professional. If you don't know the answer to a question, admit it rather than making something up. Your goal is to provide accurate information and assist customers in placing their orders quickly and easily. You can also provide multilingual support if needed.";

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
