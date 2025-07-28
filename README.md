# Meeting Recorder
 
## Self-hosting

The meeting recorder is a clone of the open source repository 'attendee' by Noah Duncan. It's a Django app made to run in a single Docker image. The only external services needed are Postgres and Redis. Directions for running locally in development mode [here](#running-in-development-mode).

## Running in development mode

- Build the Docker image: `docker compose -f dev.docker-compose.yml build` (Takes about 5 minutes)
- Create local environment variables: `docker compose -f dev.docker-compose.yml run --rm recorder-api python init_env.py > .env`
- Edit the `.env` file and enter your AWS information.
- Start all the services: `docker compose -f dev.docker-compose.yaml up`
- After the services have started, run migrations in a separate terminal tab: `docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate`
- Go to localhost:8001 in your browser and create an account
- The confirmation link will be written to the server logs in the terminal where you ran `docker compose -f dev.docker-compose.yml up`. Should look like `http://localhost:8001/accounts/confirm-email/<key>/`.
- Paste the link into your browser to confirm your account.
- You should now be able to log in, input your credentials and obtain an API key. API calls should be directed to http://localhost:8000.

## Deployment

- Build the Docker image: `docker build --platform=linux/amd64 -t vanyabrucker/transcript:transcript-meeting-recorder_1.0.1_staging -f Dockerfile .` (Takes about 5 minutes)
- Push the image to Docker Hub: `docker push vanyabrucker/transcript:transcript-meeting-recorder_1.0.1_staging`


## Calling the API

Join a meeting with a POST request to `/bots`:
```
curl -X POST http://localhost:8000/api/v1/bots \
-H 'Authorization: Token <YOUR_API_KEY>' \
-H 'Content-Type: application/json' \
-d '{"meeting_url": "https://us05web.zoom.us/j/84315220467?pwd=9M1SQg2Pu2l0cB078uz6AHeWelSK19.1", "bot_name": "My Bot"}'
```
Response:
```{"id":"bot_3hfP0PXEsNinIZmh","meeting_url":"https://us05web.zoom.us/j/4849920355?pwd=aTBpNz760UTEBwUT2mQFtdXbl3SS3i.1","state":"joining","transcription_state":"not_started"}```

The API will respond with an object that represents your bot's state in the meeting. 



Make a GET request to `/bots/<id>` to poll the bot:
```
curl -X GET http://localhost:8000/api/v1/bots/bot_3hfP0PXEsNinIZmh \
-H 'Authorization: Token <YOUR_API_KEY>' \
-H 'Content-Type: application/json'
```
Response: 
```{"id":"bot_3hfP0PXEsNinIZmh","meeting_url":"https://us05web.zoom.us/j/88669088234?pwd=AheaMumvS4qxh6UuDtSOYTpnQ1ZbAS.1","state":"ended","transcription_state":"complete"}```

When the endpoint returns a state of `ended`, it means the meeting has ended. When the `transcription_state` is `complete` it means the meeting recording has been transcribed.


Once the meeting has ended and the transcript is ready make a GET request to `/bots/<id>/transcript` to retrieve the meeting transcripts:
```
curl -X GET http://localhost:8000/api/v1/bots/bot_3hfP0PXEsNinIZmh/transcript \
-H 'Authorization: Token mpc67dedUlzEDXfNGZKyC30t6cA11TYh' \
-H 'Content-Type: application/json'
```
Response:
```
[{
"speaker_name":"Alan Turing",
"speaker_uuid":"16778240","speaker_user_uuid":"AAB6E21A-6B36-EA95-58EC-5AF42CD48AF8",
"timestamp_ms":1079,"duration_ms":7710,
"transcription":"You can totally record this, buddy. You can totally record this. Go for it, man."
},...]
```
You can also query this endpoint while the meeting is happening to retrieve partial transcripts.
