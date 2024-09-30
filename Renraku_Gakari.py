import discord
from discord.ext import commands, tasks
import os
from datetime import datetime
import pytz
import logging
import asyncio
from fastapi import FastAPI
import uvicorn
from pymongo import MongoClient
import ssl
from bson.objectid import ObjectId
import threading

# Set up logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('renraku_gakari')
logger.setLevel(logging.WARNING)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True

class RenrakuGakariBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mongo_uri = os.getenv('MONGO_URI')
        self.client = MongoClient(mongo_uri, tlsAllowInvalidCertificates=True)
        self.db = self.client.renraku_gakari_bot
        self.config_collection = self.db.guild_configs

    async def setup_hook(self):
        self.bg_task = self.loop.create_task(self.background_task())

    async def background_task(self):
        await self.wait_until_ready()
        while not self.is_closed():
            logger.debug("Background task running...")
            await asyncio.sleep(60)  # Run every minute

    def get_guild_config(self, guild_id):
        try:
            config = self.config_collection.find_one({"guild_id": str(guild_id)})
            if config:
                return config
            return {'guild_id': str(guild_id), 'monitored_channels': [], 'destination_channel': None}
        except pymongo.errors.ServerSelectionTimeoutError as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            return {'guild_id': str(guild_id), 'monitored_channels': [], 'destination_channel': None}

    def save_guild_config(self, guild_id, guild_config):
        self.config_collection.update_one(
            {"guild_id": str(guild_id)},
            {"$set": guild_config},
            upsert=True
        )

    async def close(self):
        # MongoDBクライアントを閉じる
        if hasattr(self, 'client'):
            self.client.close()
        # スーパークラスのcloseメソッドを呼び出す
        await super().close()

bot = RenrakuGakariBot(command_prefix='/', intents=intents)

# FastAPI app
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Bot is running"}

def get_current_time():
    return datetime.now(pytz.timezone('Asia/Tokyo')).strftime("%Y-%m-%d %H:%M:%S")

@bot.event
async def on_ready():
    logger.warning(f'{bot.user} has connected to Discord!')
    try:
        synced = await bot.tree.sync()
        logger.warning(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

@bot.tree.command(name="add_monitor", description="チャンネルを監視対象に追加します")
@commands.has_permissions(administrator=True)
async def add_monitor(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    guild_config = bot.get_guild_config(interaction.guild_id)
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        if channel.id not in guild_config['monitored_channels']:
            guild_config['monitored_channels'].append(channel.id)
            bot.save_guild_config(interaction.guild_id, guild_config)
            await interaction.response.send_message(f'{channel.mention} が監視対象に追加されました。', ephemeral=True)
            logger.info(f"Added {channel.name} to monitored channels in guild {interaction.guild.name}")
        else:
            await interaction.response.send_message(f'{channel.mention} は既に監視対象です。', ephemeral=True)
    else:
        await interaction.response.send_message(f'{channel.mention} は監視可能なチャンネルの種類ではありません。', ephemeral=True)

@bot.tree.command(name="remove_monitor", description="チャンネルを監視対象から削除します")
@commands.has_permissions(administrator=True)
async def remove_monitor(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    guild_config = bot.get_guild_config(interaction.guild_id)
    if channel.id in guild_config['monitored_channels']:
        guild_config['monitored_channels'].remove(channel.id)
        bot.save_guild_config(interaction.guild_id, guild_config)
        await interaction.response.send_message(f'{channel.mention} が監視対象から削除されました。', ephemeral=True)
        logger.info(f"Removed {channel.name} from monitored channels in guild {interaction.guild.name}")
    else:
        await interaction.response.send_message(f'{channel.mention} は監視対象ではありません。', ephemeral=True)

@bot.tree.command(name="set_destination", description="転送先チャンネルを設定します")
@commands.has_permissions(administrator=True)
async def set_destination(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_config = bot.get_guild_config(interaction.guild_id)
    guild_config['destination_channel'] = channel.id
    bot.save_guild_config(interaction.guild_id, guild_config)
    await interaction.response.send_message(f'転送先が {channel.mention} に設定されました。', ephemeral=True)
    logger.info(f"Set destination channel to {channel.name} in guild {interaction.guild.name}")

@bot.tree.command(name="show_config", description="現在の設定を表示します")
@commands.has_permissions(administrator=True)
async def show_config(interaction: discord.Interaction):
    guild_config = bot.get_guild_config(interaction.guild_id)
    monitored_channels = [bot.get_channel(ch_id).mention for ch_id in guild_config['monitored_channels'] if bot.get_channel(ch_id)]
    destination_channel = bot.get_channel(guild_config['destination_channel'])
    embed = discord.Embed(title="現在の設定", color=0x00ff00)
    embed.add_field(name="監視対象チャンネル", value="\n".join(monitored_channels) if monitored_channels else "なし", inline=False)
    embed.add_field(name="転送先チャンネル", value=destination_channel.mention if destination_channel else "未設定", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"Displayed config for guild {interaction.guild.name}")

async def send_notification(destination_channel, embed):
    try:
        await destination_channel.send(embed=embed)
    except discord.errors.Forbidden:
        logger.error(f"Error: Bot doesn't have permission to send messages in {destination_channel.name}")
    except Exception as e:
        logger.error(f"Error sending notification: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    guild_config = bot.get_guild_config(message.guild.id)
    monitored_channel = message.channel
    if isinstance(message.channel, discord.Thread):
        monitored_channel = message.channel.parent
    if monitored_channel.id in guild_config['monitored_channels']:
        destination_channel = bot.get_channel(guild_config['destination_channel'])
        if destination_channel:
            content = message.content[:200] + ('...' if len(message.content) > 200 else '')
            embed = discord.Embed(
                description=f"{get_current_time()}\n{discord.utils.escape_markdown(content)}",
                color=0x00ff00
            )
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
            if isinstance(message.channel, discord.Thread) and isinstance(message.channel.parent, discord.ForumChannel):
                embed.add_field(name="元の投稿", value=f"[{message.channel.parent.name} > {message.channel.name}]({message.jump_url})", inline=False)
            else:
                embed.add_field(name="元の投稿", value=f"[#{message.channel.name}]({message.jump_url})", inline=False)
            await send_notification(destination_channel, embed)
            logger.debug(f"Forwarded message from {message.channel.name} to {destination_channel.name} in guild {message.guild.name}")
    
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before, after):
    if before.author == bot.user:
        return
    guild_config = bot.get_guild_config(after.guild.id)
    monitored_channel = after.channel
    if isinstance(after.channel, discord.Thread):
        monitored_channel = after.channel.parent
    if monitored_channel.id in guild_config['monitored_channels']:
        destination_channel = bot.get_channel(guild_config['destination_channel'])
        if destination_channel:
            embed = discord.Embed(
                description=f"{get_current_time()}\nメッセージが編集されました",
                color=0xffff00
            )
            embed.set_author(name=after.author.display_name, icon_url=after.author.display_avatar.url)
            after_content = after.content[:200] + ('...' if len(after.content) > 200 else '')
            embed.add_field(name="編集後のメッセージ", value=discord.utils.escape_markdown(after_content), inline=False)
            if isinstance(after.channel, discord.Thread) and isinstance(after.channel.parent, discord.ForumChannel):
                embed.add_field(name="元の投稿", value=f"[{after.channel.parent.name} > {after.channel.name}]({after.jump_url})", inline=False)
            else:
                embed.add_field(name="元の投稿", value=f"[#{after.channel.name}]({after.jump_url})", inline=False)
            await send_notification(destination_channel, embed)
            logger.debug(f"Forwarded edited message from {after.channel.name} to {destination_channel.name} in guild {after.guild.name}")

@bot.event
async def on_message_delete(message):
    if message.author == bot.user:
        return
    guild_config = bot.get_guild_config(message.guild.id)
    monitored_channel = message.channel
    if isinstance(message.channel, discord.Thread):
        monitored_channel = message.channel.parent
    if monitored_channel.id in guild_config['monitored_channels']:
        destination_channel = bot.get_channel(guild_config['destination_channel'])
        if destination_channel:
            content = message.content[:200] + ('...' if len(message.content) > 200 else '')
            embed = discord.Embed(
                description=f"{get_current_time()}\nメッセージが削除されました",
                color=0xff0000
            )
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
            embed.add_field(name="削除されたメッセージ", value=discord.utils.escape_markdown(content), inline=False)
            if isinstance(message.channel, discord.Thread) and isinstance(message.channel.parent, discord.ForumChannel):
                embed.add_field(name="元のチャンネル", value=f"{message.channel.parent.name} > {message.channel.name}", inline=False)
            else:
                embed.add_field(name="元のチャンネル", value=f"#{message.channel.name}", inline=False)
            await send_notification(destination_channel, embed)
            logger.debug(f"Forwarded deleted message from {message.channel.name} to {destination_channel.name} in guild {message.guild.name}")

@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f"An error occurred in event {event}", exc_info=True)
    if event == 'on_interaction':
        interaction = args[0]
        if not interaction.response.is_done():
            await interaction.response.send_message("An error occurred while processing the command.", ephemeral=True)

async def main():
    bot_token = os.getenv('RENRAKU_GAKARI_TOKEN')
    mongo_uri = os.getenv('MONGO_URI')
    
    if not bot_token or not mongo_uri:
        logger.error("Bot token or MongoDB URI not found in environment variables")
        raise ValueError("Bot token or MongoDB URI not found in environment variables")

    async with bot:
        await bot.start(bot_token)

if __name__ == "__main__":
    import uvicorn
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_event_loop()

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bot.close())
        executor.shutdown(wait=True)
        loop.close()

    # FastAPIアプリケーションの起動
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv('PORT', 8080)))
