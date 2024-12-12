from typing import Dict, Optional, Set
from google.genai.chats import Chat
import google.genai as genai
import google.genai.types as gtypes
import google.generativeai as genaiold
from discord.ext import commands, voice_recv
import discord
from io import BytesIO
from pydub import AudioSegment
from time import time
import os
from dotenv import load_dotenv
import requests
from threading import Thread

load_dotenv()

genaiold.configure(api_key=os.getenv("GEMINI_API_KEY"))
client = genai.client.Client(api_key=os.getenv("GEMINI_API_KEY"))
bot = commands.Bot(command_prefix="g!", intents=discord.Intents.all())


class Sink(voice_recv.AudioSink):
    def __init__(self, chat: Chat):
        super().__init__()
        self.speaking: Set[int] = set()
        self.last_sent = 0.0
        self.chat = chat
        self.audios: Dict[int, bytes] = {}
        self.play_queue = []

    def send(self, parts):
        response = self.chat.send_message(parts)
        for part in response.candidates[0].content.parts:
            if part.text.startswith("think:"):
                print(part.text.capitalize())
                continue
            self.play(part.text)

    def play(self, content: str):
        print(f"Speak: {content}")
        res = requests.post(
            "http://localhost:5004/synthesize",
            json={"text": content, "ident": "zunda"},
        )
        if self.voice_client is None:
            return
        aus = discord.player.FFmpegPCMAudio(BytesIO(res.content), pipe=True)
        self.play_queue.append(aus)
        if len(self.play_queue) == 1:
            self._play()

    def _play(self):
        def next_p(x):
            self.play_queue.pop(0)
            self._play()

        if len(self.play_queue) > 0:
            first = self.play_queue[0]
            self.voice_client.play(first, after=next_p)

    def do_chat(self):
        with BytesIO() as io:
            merged_audio = None
            for a in self.audios.values():
                a = AudioSegment(a, sample_width=4, frame_rate=48000, channels=1)
                a = a + 2
                if merged_audio is None:
                    merged_audio = a
                else:
                    merged_audio += a
            self.audios = {}
            if merged_audio is None or merged_audio.duration_seconds < 1.0:
                return
            merged_audio.export("out.wav", format="wav")
            merged_audio.export(io, format="wav")
            io.seek(0)
            self.send(
                [
                    gtypes.Part.from_uri(
                        genaiold.upload_file(io, mime_type="audio/wav").uri,
                        "audio/wav",
                    ),
                    gtypes.Part.from_text(
                        "[SYSTEM MESSAGE] Follow the system instruction. Don't forget you should respond in the language the user speaks."
                    ),
                ]
            )

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.Member):
        if member.bot:
            return
        if member.id in self.speaking:
            self.speaking.remove(member.id)
        now = time()
        if (
            len(self.speaking) == 0
            and self.last_sent + 5.0 < now
            and self.audios is not None
        ):
            self.last_sent = now
            Thread(target=self.do_chat).start()

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member: discord.Member):
        if member.bot:
            return
        self.speaking.add(member.id)

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[discord.User], data: voice_recv.VoiceData) -> None:
        if user is not None:
            if user.id in self.audios:
                self.audios[user.id] += data.pcm
            else:
                self.audios[user.id] = data.pcm

    def cleanup(self) -> None:
        self.chat = None


sinks = {}


@bot.event
async def on_voice_member_disconnect(member: discord.Member, ssrc: int | None):
    sink = sinks.get(member.voice.channel.id)
    if sink is None:
        return
    Thread(
        target=sink.send,
        args=(f"[SYSTEM MESSAGE] {member.display_name} left the voice channel",),
    ).start()


@bot.event
async def on_voice_member_platform(member: discord.Member, platform: int | str | None):
    sink = sinks.get(member.voice.channel.id)
    if sink is None:
        return
    Thread(
        target=sink.send,
        args=(f"[SYSTEM MESSAGE] {member.display_name} joined the voice channel",),
    ).start()


@bot.command()
async def join(ctx: commands.Context):
    voice = ctx.author.voice
    if voice is None or ctx.voice_client is not None:
        return
    vc = await voice.channel.connect(cls=voice_recv.VoiceRecvClient)
    chat_session = client.chats.create(
        model="gemini-2.0-flash-exp",
        config=gtypes.GenerateContentConfig(
            temperature=1.0,
            top_p=0.95,
            top_k=40.0,
            max_output_tokens=100,
            response_mime_type="text/plain",
            system_instruction="""[SYSTEM MESSAGE] You are chatting with user on voice channel.""",
            automatic_function_calling=gtypes.AutomaticFunctionCallingConfigDict(),
            tools=[gtypes.Tool(google_search=gtypes.GoogleSearch())],
        ),
    )
    members = ",".join(
        [m.display_name for m in vc.channel.members if m.id != bot.user.id]
    )
    sink = Sink(chat_session)
    vc.listen(sink)
    Thread(
        target=sink.send,
        args=[
            f"""[SYSTEM MESSAGE] This voice channel has the following members: {members}.
Currently the voice mode only supports Japanese. Please start by introducing yourself in Japanese.
Don't do long response. Output your response very short.
If you think you don't have to respond(like when user isn't actually speaking, but their noise are on the conversation), start your response with `think:`
あなたはずんだもんです。そのため、語尾にのだを付ける傾向があります。"""
        ],
    ).start()
    sinks[voice.channel.id] = sink
    await ctx.reply("準備が完了しました。")


@bot.command()
async def dc(ctx: commands.Context):
    if ctx.voice_client is None:
        return
    del sinks[ctx.voice_client.channel.id]
    await ctx.voice_client.disconnect()


bot.run(os.getenv("DISCORD_TOKEN"))
