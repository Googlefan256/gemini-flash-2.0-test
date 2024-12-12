from typing import Dict, List, Optional, Set, Union
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
        self.audios: Dict[int, AudioSegment] = {}

    def send(self, parts):
        response = self.chat.send_message(parts)
        for part in response.candidates[0].content.parts:
            self.play(part.text)

    def play(self, content: str):
        if content.startswith("thinking:"):
            print(content.capitalize())
            return
        print(f"Speak: {content}")
        res = requests.post(
            "http://localhost:5004/synthesize",
            json={"text": content, "ident": "zunda"},
        )
        if self.voice_client is None:
            return
        self.voice_client.play(
            discord.player.FFmpegPCMAudio(BytesIO(res.content), pipe=True)
        )

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.Member):
        if member.bot:
            return
        if member.id in self.speaking:
            self.speaking.remove(member.id)
        if (
            len(self.speaking) == 0
            and self.last_sent + 7.0 < time()
            and self.audios is not None
        ):
            self.last_sent = time()
            with BytesIO() as io:
                merged_audio: List[AudioSegment] = []
                for a in self.audios.values():
                    merged_audio.extend(a)
                merged_audio: Union[AudioSegment, int] = sum(merged_audio)
                if isinstance(merged_audio, int) or merged_audio.duration_seconds < 2.0:
                    return
                self.audios = {}
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
    def on_voice_member_speaking_start(self, member: discord.Member):
        if member.bot:
            return
        self.speaking.add(member.id)

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[discord.User], data: voice_recv.VoiceData) -> None:
        s = AudioSegment(data.pcm, sample_width=4, frame_rate=48000, channels=1)
        if user is not None:
            if user.id in self.audios:
                self.audios[user.id] += s
            else:
                self.audios[user.id] = s

    def cleanup(self) -> None:
        self.chat = None


sinks = {}


@bot.event
async def on_voice_member_disconnect(member: discord.Member, ssrc: int | None):
    sink = sinks.get(member.voice.channel.id)
    if sink is None:
        return
    sink.send(f"[SYSTEM MESSAGE] {member.display_name} left the voice channel")


@bot.event
async def on_voice_member_platform(member: discord.Member, platform: int | str | None):
    sink = sinks.get(member.voice.channel.id)
    if sink is None:
        return
    sink.send(f"[SYSTEM MESSAGE] {member.display_name} joined the voice channel")


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
            max_output_tokens=512,
            response_mime_type="text/plain",
            system_instruction="""[SYSTEM MESSAGE] You are chatting with user on voice channel.
You don't have to respond to user everytime. When you think you don't have to speak something, start your response with
thinking: 
otherwise, respond in the normal way.""",
            automatic_function_calling=None,
        ),
    )
    members = ",".join(
        [m.display_name for m in vc.channel.members if m.id != bot.user.id]
    )
    sink = Sink(chat_session)
    vc.listen(sink)
    sink.send(
        f"""[SYSTEM MESSAGE] This voice channel has the following members: {members}.
Currently the voice mode only supports Japanese. Please start by introducing yourself in Japanese."""
    )
    sinks[voice.channel.id] = sink
    ctx.reply("準備が完了しました。")


@bot.command()
async def dc(ctx: commands.Context):
    if ctx.voice_client is None:
        return
    del sinks[ctx.voice_client.channel.id]
    await ctx.voice_client.disconnect()


bot.run(os.getenv("DISCORD_TOKEN"))
