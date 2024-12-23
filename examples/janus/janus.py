import argparse
import asyncio
import logging
import random
import string
import time
import cv2
import pyaudio
import aiohttp

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRecorder

pcs = set()


def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))

# TODO: test user device and if no device usable, use customized track in the very first demo.
class AudioReceiver:
    def __init__(self, rate=48000, channels=2, chunk_size=960):
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(format=pyaudio.paInt16,
                                  channels=channels,
                                  rate=rate,
                                  output=True,
                                  frames_per_buffer=chunk_size,
                                  output_device_index=None)

    async def handle_track(self, track):
        frame_count = 0
        while True:
            try:
                audio_frame = await track.recv()
                print(f"Received audio frame with pts {audio_frame.pts}, time_base {audio_frame.time_base}")
                frame_count += 1
                audio_data = audio_frame.to_ndarray()
                self.stream.write(audio_data.tobytes())
            except Exception as e:
                print(f"Error receiving audio: {str(e)}")
                break

class VideoReceiver:
    def __init__(self, id):
        self.track = None
        self.publisher_id = id

    async def handle_track(self, track):
        self.track = track
        frame_count = 0
        while True:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=15.0)
                frame = frame.to_ndarray(format="bgr24")
                frame_count += 1
                print(f"Received video frame {frame_count}")
                cv2.imshow(f"Frame_{self.publisher_id}", frame)

                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
            except asyncio.TimeoutError:
                print("Timeout waiting for frame, continuing...")
            except Exception as e:
                print(f"Error receiving video: {str(e)}")
                break


class JanusPlugin:
    def __init__(self, session, url):
        self._queue = asyncio.Queue()
        self._session = session
        self._url = url

    async def send(self, payload):
        """
        send a message to the plugin and wait for response.
        """
        # prepare message
        message = {"janus": "message", "transaction": transaction_id()}
        message.update(payload)
        
        # send message and wait for response
        async with self._session._http.post(self._url, json=message) as response:
            data = await response.json()
            assert data["janus"] == "ack"

        # wait for response
        response = await self._queue.get()
        assert response["transaction"] == message["transaction"]
        return response


class JanusSession:
    def __init__(self, url):
        self._http = None           # a aiohttp.ClientSession() to communicate with janus server
        self._poll_task = None
        self._plugins = {}          # a set of plugins attached to the session
        self._root_url = url
        self._session_url = None    # the url of the session

    async def attach(self, plugin_name: str) -> JanusPlugin:
        """
        Attach a plugin to the session.
        """
        # Prepare message
        message = {
            "janus": "attach",
            "plugin": plugin_name,
            "transaction": transaction_id(),
        }
        
        # Send message and wait for response
        async with self._http.post(self._session_url, json=message) as response:
            data = await response.json()
            assert data["janus"] == "success"
            plugin_id = data["data"]["id"]
            plugin = JanusPlugin(self, self._session_url + "/" + str(plugin_id))
            # add the plugin to the set of plugins
            self._plugins[plugin_id] = plugin
            return plugin

    async def create(self):
        """
        Create a new session. After that the client is connected with the janus server?
        """
        # prepare message and send via http
        self._http = aiohttp.ClientSession()
        message = {"janus": "create", "transaction": transaction_id()}
        
        # wait for response
        async with self._http.post(self._root_url, json=message) as response:
            data = await response.json()
            assert data["janus"] == "success"
            session_id = data["data"]["id"]
            # record the session url based on the response
            self._session_url = self._root_url + "/" + str(session_id)

        self._poll_task = asyncio.ensure_future(self._poll())

    async def destroy(self):
        """
        Destroy the session.
        """
        # cancel the poll task, which is a coroutine
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

        # send destroy message to janus server
        if self._session_url:
            message = {"janus": "destroy", "transaction": transaction_id()}
            async with self._http.post(self._session_url, json=message) as response:
                data = await response.json()
                assert data["janus"] == "success"
            self._session_url = None

        # close the _http connection
        if self._http:
            await self._http.close()
            self._http = None

    async def _poll(self):
        """
        Poll for messages.
        """
        while True:
            params = {"maxev": 1, "rid": int(time.time() * 1000)}
            # get response regu
            async with self._http.get(self._session_url, params=params) as response:
                data = await response.json()
                if data["janus"] == "event":
                    plugin = self._plugins.get(data["sender"], None)
                    if plugin:
                        await plugin._queue.put(data)
                    else:
                        print(data)


async def publish(plugin, player, audio_player=None):
    """
    Send video to the room.
    """
    # prepare peer connection
    pc = RTCPeerConnection()
    pcs.add(pc)

    # configure audio and video media
    # Taojie: the logic is actually very similar.
    #           The track can be either a Track or a MediaPlayer(WOW!)
    # media = {"audio": False, "video": True}
    # if player and player.audio:
    #     pc.addTrack(player.audio)
    #     media["audio"] = True
    # if player and player.video:
    #     pc.addTrack(player.video)
    # else:
    #     pc.addTrack(VideoStreamTrack())
    
    # Taojie: we ensure that player and audio_player are not None
    # add video and audio track to the peer connection
    pc.addTrack(player.video)
    pc.addTrack(audio_player.audio)
    media = {"audio": True, "video": True}

    # prepare offer
    await pc.setLocalDescription(await pc.createOffer())
    request = {"request": "configure"}
    request.update(media)  # request = {"request": "configure", "audio": False, "video": True}
    
    # send the request via plugin and get response
    response = await plugin.send(
        {
            "body": request,
            "jsep": {
                "sdp": pc.localDescription.sdp,
                "trickle": False,
                "type": pc.localDescription.type,
            },
        }
    )

    # apply answer
    await pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=response["jsep"]["sdp"], type=response["jsep"]["type"]
        )
    )


async def subscribe(session, room, feed, recorder: VideoReceiver, audio_recorder:AudioReceiver):
    # prepare peer connection
    pc = RTCPeerConnection()
    pcs.add(pc)

    # define behavior when a track is received
    @pc.on("track")
    async def on_track(track):
        print("Track %s received" % track.kind)
        if track.kind == "video":
            # recorder.addTrack(track)
            asyncio.ensure_future(recorder.handle_track(track))
        if track.kind == "audio":
            # recorder.addTrack(track)
            asyncio.ensure_future(audio_recorder.handle_track())

    # subscribe: send join request to the plugin
    plugin = await session.attach("janus.plugin.videoroom")
    response = await plugin.send(
        {"body": {"request": "join", "ptype": "subscriber", "room": room, "feed": feed}}
    )

    # apply offer
    await pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=response["jsep"]["sdp"], type=response["jsep"]["type"]
        )
    )

    # send answer
    await pc.setLocalDescription(await pc.createAnswer())
    response = await plugin.send(
        {
            "body": {"request": "start"},
            "jsep": {
                "sdp": pc.localDescription.sdp,
                "trickle": False,
                "type": pc.localDescription.type,
            },
        }
    )
    # await recorder.start()


# Taojie: to maintian consistency, player here refers to the video player; similar for audio_recorder
async def run(player, audio_player,  recorder, audio_recorder,  room, session: JanusSession):
    # create a janus session, connect to the server
    await session.create()

    # join video room
    # attach a plugin to the session
    plugin = await session.attach("janus.plugin.videoroom")
    
    # send join message to the plugin and get publishers list
    response = await plugin.send(
        {
            "body": {
                "display": "aiortc",
                "ptype": "publisher",
                "request": "join",
                "room": room,
            }
        }
    )
    publishers = response["plugindata"]["data"]["publishers"]
    for publisher in publishers:
        print("id: %(id)s, display: %(display)s" % publisher)
                
    # send video
    if player:    
        await publish(plugin=plugin, player=player, audio_player=audio_player)

    # receive video if publiser exists
    if recorder is not None and publishers:
        # await subscribe(
        #     session=session, room=room, feed=publishers[0]["id"], recorder=recorder
        # )
        for i, publisher in enumerate(publishers):
            recorder = VideoReceiver(i)
            await subscribe(
                session=session, room=room, feed=publisher["id"], recorder=recorder, audio_recorder=audio_recorder
            )

    # exchange media for 10 minutes
    print("Exchanging media")
    await asyncio.sleep(1200)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Janus")
    parser.add_argument(
        "url",
        help="Janus root URL, e.g. http://localhost:8088/janus",
        nargs="?",
        default="http://10.16.56.14:8088/janus"
    )
    parser.add_argument(
        "--room",
        type=int,
        default=1234,
        help="The video room ID to join (default: 1234).",
    )
    parser.add_argument("--play-from", help="Read the media from a file and sent it.")
    parser.add_argument("--record-to", help="Write received media to a file.")
    parser.add_argument(
        "--play-without-decoding",
        help=(
            "Read the media without decoding it (experimental). "
            "For now it only works with an MPEGTS container with only H.264 video."
        ),
        action="store_true",
    )
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # create signaling and peer connection
    session = JanusSession(args.url)

    # prepare sender and recorder media
    # Taojie: if we set file as "video=HP True Vision 5MP Camera", it will use the camera to record the video
    # player = MediaPlayer(args.play_from, decode=not args.play_without_decoding) if args.play_from else None
    # recorder = MediaRecorder(args.record_to) if args.record_to else None
    if args.play_from:
        options = {"framerate": "30", "video_size": "640x480"}
        video_player = MediaPlayer(
                        # "video=HP True Vision 5MP Camera", format="dshow", options=options
                        "video=HP True Vision 5MP Camera", format="dshow", options=options
                    )
        audio_player = MediaPlayer("audio=麦克风阵列 (适用于数字麦克风的英特尔® 智音技术)", format="dshow")
    else:
        video_player = None
    recorder = VideoReceiver(-1)
    audio_recorder = AudioReceiver()

    # Run!
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(
            run(player=video_player, audio_player=audio_player, recorder=recorder, audio_recorder=audio_recorder, room=args.room, session=session)
        )
    except KeyboardInterrupt:
        pass
    finally:
        if recorder is not None:
            pass
            # Taojie: recorder.stop() is not defined
            # loop.run_until_complete(recorder.stop())
        loop.run_until_complete(session.destroy())

        # close peer connections
        coros = [pc.close() for pc in pcs]
        loop.run_until_complete(asyncio.gather(*coros))
        print("video sending ended")
