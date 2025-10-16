'use strict';

const heygen_API = {
  apiKey: 'Yjk4YzA0ZDY4MjQ0NDM5ZGI5NWRjNmFhNjA0N2Y2M2QtMTc2MDEwODY4MA==',
  serverUrl: 'https://api.heygen.com',
};

const statusElement = document.querySelector('#status');

const apiKey = heygen_API.apiKey;
const SERVER_URL = heygen_API.serverUrl;

if (apiKey === 'YourApiKey' || SERVER_URL === '') {
  alert('Please enter your API key and server URL in the api.json file');
}

let sessionInfo = null;
let peerConnection = null;
// Recording state
let mediaRecorder = null;
let recordingChunks = [];
let recordingStream = null;
let isRecording = false;

function updateStatus(statusElement, message) {
  statusElement.innerHTML += message + '<br>';
  statusElement.scrollTop = statusElement.scrollHeight;
}

// const menuElement = document.querySelector('#menu');
// function updateMenu(resp) {
//   console.log(resp);
//   if (!resp || !resp.menu || resp.menu.length === 0) {
//     menuElement.innerHTML = '<i>No menu available</i>';
//     return;
//   }

//   let html = '<ul>';
//   resp.menu.forEach((item) => {
//     html += `<li><b>${item.name}</b>: ${item.description} - <i>${item.price}</i></li>`;
//   });
//   html += '</ul>';

//   menuElement.innerHTML = html;
// }
updateStatus(statusElement, 'Please click the new button to create the stream first.');

function onMessage(event) {
  const message = event.data;
  console.log('Received message:', message);
}

// Create a new WebRTC session when clicking the "New" button
async function createNewSession() {
  updateStatus(statusElement, 'Creating new session... please wait');

  const avatar = avatarID.value;
  const voice = voiceID.value;

  // call the new interface to get the server's offer SDP and ICE server to create a new RTCPeerConnection
  sessionInfo = await newSession('low', avatar, voice);
  const { sdp: serverSdp, ice_servers2: iceServers } = sessionInfo;

  // Create a new RTCPeerConnection
  peerConnection = new RTCPeerConnection({ iceServers: iceServers });

  // When audio and video streams are received, display them in the video element
  peerConnection.ontrack = (event) => {
    console.log('Received the track');
    if (event.track.kind === 'audio' || event.track.kind === 'video') {
      mediaElement.srcObject = event.streams[0];
    }
  };

  // When receiving a message, display it in the status element
  peerConnection.ondatachannel = (event) => {
    const dataChannel = event.channel;
    dataChannel.onmessage = onMessage;
  };

  // Set server's SDP as remote description
  const remoteDescription = new RTCSessionDescription(serverSdp);
  await peerConnection.setRemoteDescription(remoteDescription);

  updateStatus(statusElement, 'Session creation completed');
  updateStatus(statusElement, 'Now.You can click the start button to start the stream');
}

// Start session and display audio and video when clicking the "Start" button
async function startAndDisplaySession() {
  if (!sessionInfo) {
    updateStatus(statusElement, 'Please create a connection first');
    return;
  }

  updateStatus(statusElement, 'Starting session... please wait');

  // Create and set local SDP description
  const localDescription = await peerConnection.createAnswer();
  await peerConnection.setLocalDescription(localDescription);

 // When ICE candidate is available, send to the server
  peerConnection.onicecandidate = ({ candidate }) => {
    console.log('Received ICE candidate:', candidate);
    if (candidate) {
      handleICE(sessionInfo.session_id, candidate.toJSON());
    }
  };

  // When ICE connection state changes, display the new state
  peerConnection.oniceconnectionstatechange = (event) => {
    updateStatus(
      statusElement,
      `ICE connection state changed to: ${peerConnection.iceConnectionState}`,
    );
  };



  // Start session
  await startSession(sessionInfo.session_id, localDescription);

  var receivers = peerConnection.getReceivers();
  
  receivers.forEach((receiver) => {
    receiver.jitterBufferTarget = 500
  });

   updateStatus(statusElement, 'Session started successfully');
}

const taskInput = document.querySelector('#taskInput');

// When clicking the "Send Task" button, get the content from the input field, then send the tas
async function repeatHandler() {
  if (!sessionInfo) {
    updateStatus(statusElement, 'Please create a connection first');

    return;
  }
  updateStatus(statusElement, 'Sending task... please wait');
  const text = taskInput.value;
  if (text.trim() === '') {
    alert('Please enter a task');
    return;
  }

  const resp = await repeat(sessionInfo.session_id, text);

  updateStatus(statusElement, 'Task sent successfully');
}

async function talkHandler() {
  if (!sessionInfo) {
    updateStatus(statusElement, 'Please create a connection first');
    return;
  }
  const prompt = taskInput.value; // Using the same input for simplicity
  if (prompt.trim() === '') {
    alert('Please enter a prompt for the LLM');
    return;
  }

  updateStatus(statusElement, 'Talking to LLM... please wait');

  // try {
  //   const text = await talkToOpenAI(prompt)
  //   console.log(text)
  //   if (text) {
  //     // Send the AI's response to Heygen's streaming.task API
  //     const resp = await repeat(sessionInfo.session_id, text);
  //     updateMenu(text);
  //     updateStatus(statusElement, 'LLM response sent successfully');
  //   } else {
  //     updateStatus(statusElement, 'Failed to get a response from AI');
  //   }
  // } catch (error) {
  //   console.error('Error talking to AI:', error);
  //   updateStatus(statusElement, 'Error talking to AI');
  // }
  try {
    const text = await talkToOpenAI(prompt);
    console.log(text);

    if (text) {
        // Send the AI's full conversational response to Heygen's streaming API
        const resp = await repeat(sessionInfo.session_id, text);

        // --- FIX STARTS HERE ---
        // 1. Define a helper function to extract the order summary
        const currentOrderSummary = extractOrderSummary(text);

        // 2. Pass the extracted order summary to a new function (or your existing one)
        // Note: You should rename 'updateMenu' to something like 'updateOrderSummary'
        if (currentOrderSummary) {
            updateOrderSummary(currentOrderSummary); 
        } else {
            // Optional: Handle case where no summary was found (e.g., general conversation)
            console.warn("LLM response did not contain a Current Order Summary.");
        }
        // --- FIX ENDS HERE ---

        updateStatus(statusElement, 'LLM response sent successfully');
    } else {
        updateStatus(statusElement, 'Failed to get a response from AI');
    }
  } catch (error) {
      console.error('Error talking to AI:', error);
      updateStatus(statusElement, 'Error talking to AI');
  }
}
function extractOrderSummary(llmResponseText) {
  // const startTag = "Current Order Summary:";
  // const startIndex = llmResponseText.indexOf(startTag);

  // if (startIndex === -1) {
  //     return null;
  // }

  // // Find the starting point (including the tag)
  // const summaryStart = llmResponseText.substring(startIndex);
  
  // // The summary likely runs to the end of the string, or until the next major section/line break
  // // For simplicity, we'll return the rest of the text starting from the tag.
  // return summaryStart.trim();
  const regex = /Sure (.+?) is (\$.+?) would you like anything else\?/i;

    // 2. Execute the regex on the response text
  const match = llmResponseText.match(regex);

  // 3. Check for a successful match
  if (match && match.length === 3) {
      // match[0] is the entire matched string
      // match[1] is the content of the first capture group (the item name)
      // match[2] is the content of the second capture group (the price)
      return {
          item: match[1].trim(),
          price: match[2].trim()
      };
  }

  // Return null if the required conversational pattern was not found
  return null;
}


/**
* Updates the display element with the current order.
* @param {string} summaryText - The text containing "Current Order Summary:..."
*/
function updateOrderSummary(itemDetails) {
  const orderElement = document.querySelector('#menu'); 
  const totalElement = document.querySelector('#orderTotal');
  console.log("Updating order summary with:", itemDetails);
  // 1. Guard check to ensure a valid object was passed
  if (!itemDetails || typeof itemDetails !== 'object' || !itemDetails.item || !itemDetails.price) {
      console.error("Failed to update order: Invalid item details provided.");
      // If the item wasn't added (e.g., LLM returned the apology), we just exit.
      return;
  }
  const newItemPrice = parsePrice(itemDetails.price);

  //   // 3. Get the current total from the DOM element
  //   //    We check for existing text, parse it, or default to 0.
  const currentTotalText = totalElement.textContent || totalElement.innerText || '$0.00';
  const currentTotal = parsePrice(currentTotalText);

  // // 4. Calculate the new total (Use toFixed for accurate currency math)
  const newTotal = (currentTotal + newItemPrice);

  // // 5. Update the #orderTotal element with the new formatted total
  // //    We explicitly set it to a dollar string with two decimal places.
  totalElement.textContent = `$${newTotal.toFixed(2)}`;
  // 2. Clear old logic (split, slice, filter) and replace with object access
  
  // Build the HTML for the single newly added item
  let html = '<ul>';
  // Use the properties from the object: itemDetails.item and itemDetails.price
  html += `<li>Added: <b>${itemDetails.item}</b> - ${itemDetails.price}</li>`;
  html += '</ul>';

  // 3. Use += to append the new item to the existing list on the screen
  orderElement.innerHTML += html;
}

function parsePrice(priceString) {
  // Remove the dollar sign, trim whitespace, and convert to a float
  const cleanPrice = priceString.replace('$', '').trim();
  return parseFloat(cleanPrice) || 0; // Returns 0 if parsing fails
}
// Record speech and send to Trussed STT endpoint, then send the transcribed text to Heygen's streaming.task API
async function recordSpeechHandler() {
  const recordBtn = document.querySelector('#recordBtn');

  if (!sessionInfo) {
    updateStatus(statusElement, 'Please create a connection first');
    return;
  }

  try {
    // Toggle: start recording if not recording, otherwise stop
    if (!isRecording) {
      // Start recording
      recordingChunks = [];

      // Request microphone access
      recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });

      // Create MediaRecorder (fall back to default if mimeType unsupported)
      let options = {};
      try {
        // prefer webm/opus for size and compatibility
        options = { mimeType: 'audio/webm' };
        mediaRecorder = new MediaRecorder(recordingStream, options);
      } catch (err) {
        // fallback without explicit mimeType
        mediaRecorder = new MediaRecorder(recordingStream);
      }

      mediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) recordingChunks.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        updateStatus(statusElement, 'Recording stopped. Preparing upload...');

        const blob = new Blob(recordingChunks, { type: recordingChunks[0]?.type || 'audio/webm' });

        const form = new FormData();
        // server expects the multipart field name 'audio' (see server.js upload.single('audio'))
        form.append('audio', blob, 'speech.webm');
        form.append('model', 'whisper-1');

        updateStatus(statusElement, 'Uploading audio for transcription...');

        try {
          const resp = await fetch('http://localhost:3000/speech-to-text', {
            method: 'POST',
            body: form,
          });

          if (!resp.ok) {
            const txt = await resp.text();
            throw new Error(txt || resp.statusText);
          }

          const data = await resp.json();
          const text = data.text;

          updateStatus(statusElement, `Transcription: ${text || '[empty]'}`);

          if (text && sessionInfo) {
            // send the transcribed text to Heygen streaming.task
            await repeat(sessionInfo.session_id, text);
            updateStatus(statusElement, 'Sent transcribed text to avatar');
          }
        } catch (err) {
          console.error('STT upload/transcription error:', err);
          updateStatus(statusElement, 'Error transcribing audio');
        } finally {
          // cleanup
          try {
            if (recordingStream) recordingStream.getTracks().forEach((t) => t.stop());
          } catch (e) {
            // ignore
          }
          recordingStream = null;
          mediaRecorder = null;
          isRecording = false;
          if (recordBtn) recordBtn.textContent = 'Record';
        }
      };

      mediaRecorder.start();
      isRecording = true;
      if (recordBtn) recordBtn.textContent = 'Stop';
      updateStatus(statusElement, 'Recording... click again to stop');
    } else {
      // stop recording
      if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop();
        updateStatus(statusElement, 'Stopping recording...');
      }
    }
  } catch (error) {
    console.error('Error recording speech:', error);
    updateStatus(statusElement, 'Error recording speech');
    // clean up on error
    try {
      if (recordingStream) recordingStream.getTracks().forEach((t) => t.stop());
    } catch (e) {}
    recordingStream = null;
    mediaRecorder = null;
    isRecording = false;
    if (recordBtn) recordBtn.textContent = 'Record';
  }
}

// when clicking the "Close" button, close the connection
async function closeConnectionHandler() {
  if (!sessionInfo) {
    updateStatus(statusElement, 'Please create a connection first');
    return;
  }

  renderID++;
  hideElement(canvasElement);
  hideElement(bgCheckboxWrap);
  mediaCanPlay = false;

  updateStatus(statusElement, 'Closing connection... please wait');
  try {
    // Close local connection
    peerConnection.close();
    // Call the close interface
    const resp = await stopSession(sessionInfo.session_id);

    console.log(resp);
  } catch (err) {
    console.error('Failed to close the connection:', err);
  }
  updateStatus(statusElement, 'Connection closed successfully');
}

document.querySelector('#newBtn').addEventListener('click', createNewSession);
document.querySelector('#startBtn').addEventListener('click', startAndDisplaySession);
document.querySelector('#repeatBtn').addEventListener('click', repeatHandler);
document.querySelector('#closeBtn').addEventListener('click', closeConnectionHandler);
document.querySelector('#talkBtn').addEventListener('click', talkHandler);
document.querySelector('#recordBtn').addEventListener('click', recordSpeechHandler);


// new session
async function newSession(quality, avatar_name, voice_id) {
  const response = await fetch(`${SERVER_URL}/v1/streaming.new`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Api-Key': apiKey,
    },
    body: JSON.stringify({
      quality,
      avatar_name,
      voice: {
        voice_id: voice_id,
      },
    }),
  });
  if (response.status === 500) {
    console.error('Server error');
    updateStatus(
      statusElement,
      'Server Error. Please ask the staff if the service has been turned on',
    );

    throw new Error('Server error');
  } else {
    const data = await response.json();
    console.log(data.data);
    return data.data;
  }
}

// start the session
async function startSession(session_id, sdp) {
  const response = await fetch(`${SERVER_URL}/v1/streaming.start`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Api-Key': apiKey,
    },
    body: JSON.stringify({ session_id, sdp }),
  });
  if (response.status === 500) {
    console.error('Server error');
    updateStatus(
      statusElement,
      'Server Error. Please ask the staff if the service has been turned on',
    );
    throw new Error('Server error');
  } else {
    const data = await response.json();
    return data.data;
  }
}

// submit the ICE candidate
async function handleICE(session_id, candidate) {
  const response = await fetch(`${SERVER_URL}/v1/streaming.ice`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Api-Key': apiKey,
    },
    body: JSON.stringify({ session_id, candidate }),
  });
  if (response.status === 500) {
    console.error('Server error');
    updateStatus(
      statusElement,
      'Server Error. Please ask the staff if the service has been turned on',
    );
    throw new Error('Server error');
  } else {
    const data = await response.json();
    return data;
  }
}

async function talkToOpenAI(prompt) {
  const response = await fetch(`http://localhost:3000/openai/complete`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ prompt }),
  });
  if (response.status === 500) {
    console.error('Server error');
    updateStatus(
      statusElement,
      'Server Error. Please make sure to set the openai api key',
    );
    throw new Error('Server error');
  } else {
    const data = await response.json();
    return data.text;
  }
}

// repeat the text
async function repeat(session_id, text) {
  const response = await fetch(`${SERVER_URL}/v1/streaming.task`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Api-Key': apiKey,
    },
    body: JSON.stringify({ session_id, text }),
  });
  if (response.status === 500) {
    console.error('Server error');
    updateStatus(
      statusElement,
      'Server Error. Please ask the staff if the service has been turned on',
    );
    throw new Error('Server error');
  } else {
    const data = await response.json();
    return data.data;
  }
}

// stop session
async function stopSession(session_id) {
  const response = await fetch(`${SERVER_URL}/v1/streaming.stop`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Api-Key': apiKey,
    },
    body: JSON.stringify({ session_id }),
  });
  if (response.status === 500) {
    console.error('Server error');
    updateStatus(statusElement, 'Server Error. Please ask the staff for help');
    throw new Error('Server error');
  } else {
    const data = await response.json();
    return data.data;
  }
}

const removeBGCheckbox = document.querySelector('#removeBGCheckbox');
removeBGCheckbox.addEventListener('click', () => {
  const isChecked = removeBGCheckbox.checked; // status after click

  if (isChecked && !sessionInfo) {
    updateStatus(statusElement, 'Please create a connection first');
    removeBGCheckbox.checked = false;
    return;
  }

  if (isChecked && !mediaCanPlay) {
    updateStatus(statusElement, 'Please wait for the video to load');
    removeBGCheckbox.checked = false;
    return;
  }

  if (isChecked) {
    hideElement(mediaElement);
    showElement(canvasElement);

    renderCanvas();
  } else {
    hideElement(canvasElement);
    showElement(mediaElement);

    renderID++;
  }
});

let renderID = 0;
function renderCanvas() {
  if (!removeBGCheckbox.checked) return;
  hideElement(mediaElement);
  showElement(canvasElement);

  canvasElement.classList.add('show');

  const curRenderID = Math.trunc(Math.random() * 1000000000);
  renderID = curRenderID;

  const ctx = canvasElement.getContext('2d', { willReadFrequently: true });

  if (bgInput.value) {
    canvasElement.parentElement.style.background = bgInput.value?.trim();
  }

  function processFrame() {
    if (!removeBGCheckbox.checked) return;
    if (curRenderID !== renderID) return;

    canvasElement.width = mediaElement.videoWidth;
    canvasElement.height = mediaElement.videoHeight;

    ctx.drawImage(mediaElement, 0, 0, canvasElement.width, canvasElement.height);
    ctx.getContextAttributes().willReadFrequently = true;
    const imageData = ctx.getImageData(0, 0, canvasElement.width, canvasElement.height);
    const data = imageData.data;

    for (let i = 0; i < data.length; i += 4) {
      const red = data[i];
      const green = data[i + 1];
      const blue = data[i + 2];

      // You can implement your own logic here
      if (isCloseToGreen([red, green, blue])) {
        // if (isCloseToGray([red, green, blue])) {
        data[i + 3] = 0;
      }
    }

    ctx.putImageData(imageData, 0, 0);

    requestAnimationFrame(processFrame);
  }

  processFrame();
}

function isCloseToGreen(color) {
  const [red, green, blue] = color;
  return green > 90 && red < 90 && blue < 90;
}

function hideElement(element) {
  element.classList.add('hide');
  element.classList.remove('show');
}
function showElement(element) {
  element.classList.add('show');
  element.classList.remove('hide');
}

const mediaElement = document.querySelector('#mediaElement');
let mediaCanPlay = false;
mediaElement.onloadedmetadata = () => {
  mediaCanPlay = true;
  mediaElement.play();

  showElement(bgCheckboxWrap);
};
const canvasElement = document.querySelector('#canvasElement');

const bgCheckboxWrap = document.querySelector('#bgCheckboxWrap');
const bgInput = document.querySelector('#bgInput');
bgInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    renderCanvas();
  }
});
