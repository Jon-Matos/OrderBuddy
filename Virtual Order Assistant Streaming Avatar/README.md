# Virtual Order Assistant Streaming Avatar Demo
### Which Avatars can I use with this project?

By default, there are several Public Avatars that can be used in Streaming. (AKA Streaming Avatars.) You can find the Avatar IDs for these Public Avatars by navigating to app.heygen.com/streaming-avatar and clicking 'Select Avatar'.

### Which voices can I use?

Most of HeyGen's AI Voices can be used with the Streaming API. To find the Voice IDs that you can use, please use the List Voices v2 endpoint from HeyGen: https://docs.heygen.com/reference/list-voices-v2

## Using the Demo

1. Open the web browser and enter the `http://localhost:3000`to start the demo.
2. Click the "New" button to create a new session. The status updates will be displayed on the screen.
3. After the session is created successfully, click the "Start" button to start streaming the avatar.
4. To send a task to the avatar, type the text in the provided input field and click the "Repeat Text" button.
5. In order to use Talk mode, set your **OpenAI** key in **server.js** before starting the server and click "Talk" button
6. Once done, click the "Close Connection" button to close the session.
