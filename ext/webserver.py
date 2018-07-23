import discord

from aiohttp import web
import auth_token
import aiosqlite
import xmltodict
import datetime
import re
from apscheduler.schedulers.asyncio import AsyncIOScheduler


class Webserver:
    """A webserver for handling notifications"""
    def __init__(self, bot):
        self.bot = bot
        self.cleanr = re.compile('<.*?>')

        # create the application and add routes
        self.app = web.Application()
        self.app.add_routes([web.get("/" + auth_token.google_callback_verification, self.googleverification)])
        self.app.add_routes([web.post("/youtube", self.youtube)])
        self.app.add_routes([web.get("/youtube", self.youtubeverification)])
        self.app.add_routes([web.post("/twitch", self.twitch)])
        self.app.add_routes([web.get("/twitch", self.twitchverification)])
        self.app.add_routes([web.post("/surrenderat20", self.surrenderat20)])
        self.app.add_routes([web.get("/surrenderat20", self.surrenderat20verification)])

        # push notification run out after a specified time so I need to refresh them regularly
        self.scheduler = AsyncIOScheduler(event_loop=self.bot.loop)
        self.scheduler.add_job(self.refresh_subscriptions, "interval", days=1, id="refresher", replace_existing=True, next_run_time=datetime.datetime.now())
        self.scheduler.add_job(self.ping_feedburner, "interval", minutes=3, id="pinger", replace_existing=True)
        self.scheduler.start()

        # create the run task
        self.bot.run_webserver = self.bot.loop.create_task(self.run())

    # run the webserver
    async def run(self):
        await self.bot.wait_until_ready()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner)
        await self.site.start()

    # will be called by the scheduler above
    # goes through all twitch and youtube channels in the database
    # and refresh their subscription
    async def refresh_subscriptions(self):
        async with aiosqlite.connect("data.db") as db:
            cursor = await db.execute("SELECT DISTINCT TwitchChannel FROM TwitchSubscriptions")
            async for row in cursor:
                ID = row[0]
                parsingChannelUrl = "https://api.twitch.tv/helix/webhooks/hub"
                parsingChannelHeader = {'Client-ID': auth_token.twitch}
                parsingChannelQueryString = {"hub.mode": "subscribe", "hub.callback": auth_token.server_url + "/twitch",
                                             "hub.topic": "https://api.twitch.tv/helix/streams?user_id=" + ID, "hub.lease_seconds": 864000}
                async with self.bot.session.post(parsingChannelUrl, headers=parsingChannelHeader, params=parsingChannelQueryString) as resp:
                    if resp.status != 202:
                        print(resp.text)
            await cursor.close()

            cursor = await db.execute("SELECT DISTINCT YoutubeChannel FROM YoutubeSubscriptions")
            async for row in cursor:
                ID = row[0]
                parsingChannelUrl = "https://pubsubhubbub.appspot.com/subscribe"
                parsingChannelQueryString = {"hub.mode": "subscribe", "hub.callback": auth_token.server_url + "/youtube",
                                             "hub.topic": "https://www.youtube.com/xml/feeds/videos.xml?channel_id=" + ID, "hub.lease_seconds": 864000}
                async with self.bot.session.post(parsingChannelUrl, headers=parsingChannelHeader, params=parsingChannelQueryString) as resp:
                    if resp.status != 202:
                        print(resp.text)
            await cursor.close()

    # pings feedburner to update feed
    async def ping_feedburner(self):
        parsingChannelUrl = "http://feedburner.google.com/fb/a/pingSubmit?bloglink=https%3A%2F%2Ffeeds.feedburner.com%2Fsurrenderat20%2FCqWw"
        async with self.bot.session.get(parsingChannelUrl) as resp:
            if resp.status != 200:
                print(resp.text)

    # handler for post requests to the /youtube route
    async def youtube(self, request):
        obj = xmltodict.parse(await request.text())
        # to check if the notification is about a video being deleted
        try:
            # if the request contains a "at:deleted-entry" parameter it is
            # otherwise a keyerror is raised which will be ignored to continue on normally
            obj["feed"]["at:deleted-entry"]
            parsingChannelUrl = "https://www.googleapis.com/youtube/v3/channels"
            parsingChannelQueryString = {"part": "statistics", "id": obj["feed"]["at:deleted-entry"]["at:by"]["uri"].split("/")[-1], "maxResults": "1",
                                         "key": auth_token.google}
            async with self.bot.session.get(parsingChannelUrl, params=parsingChannelQueryString) as resp:
                ch = await resp.json()
            async with aiosqlite.connect("data.db") as db:
                await db.execute("UPDATE YoutubeChannels SET VideoCount=? WHERE ID=?", (int(ch["items"][0]["statistics"]["videoCount"]), ch["items"][0]["id"]))
                await db.commit()
            return web.Response()
        except KeyError:
            pass

        # getting the video data
        parsingChannelUrl = "https://www.googleapis.com/youtube/v3/videos"
        parsingChannelQueryString = {"part": "snippet", "id": obj["feed"]["entry"]["yt:videoId"], "maxResults": "1",
                                     "key": auth_token.google}
        async with self.bot.session.get(parsingChannelUrl, params=parsingChannelQueryString) as resp:
            v = await resp.json()
        if len(v["items"]) == 0:
            # video not found, probably deleted already
            return web.Response()
        video = v["items"][0]["snippet"]

        # getting channel data
        parsingChannelUrl = "https://www.googleapis.com/youtube/v3/channels"
        parsingChannelQueryString = {"part": "snippet,statistics", "id": video["channelId"], "maxResults": "1",
                                     "key": auth_token.google}
        async with self.bot.session.get(parsingChannelUrl, params=parsingChannelQueryString) as resp:
            ch = await resp.json()
        channel_obj = ch["items"][0]

        # creating message embed
        emb = discord.Embed(title=video["title"],
                            description=video["channelTitle"],
                            url=obj["feed"]["entry"]["link"]["@href"],
                            color=discord.Colour.red())
        emb.timestamp = datetime.datetime.utcnow()
        emb.set_image(url=video["thumbnails"]["default"]["url"])
        emb.set_footer(icon_url=channel_obj["snippet"]["thumbnails"]["default"]["url"], text="Youtube")

        # check if it's a video or a livestream
        # if it is neither it is a livestream announcement which will be ignored
        if video["liveBroadcastContent"] == "none":
            announcement = "New Video live!"
        elif video["liveBroadcastContent"] == "live":
            announcement = video["channelTitle"] + " is now live!"
        else:
            return web.Response()

        async with aiosqlite.connect("data.db") as db:
            # if it is a livestream the bot shouldn't announce a livestream more than once in an hour
            # to keep channels from getting spammed from stream restarts
            if video["liveBroadcastContent"] == "live":
                cursor = await db.execute("SELECT LastLive FROM YoutubeChannels WHERE ID=?", (obj["feed"]["entry"]["yt:channelId"],))
                dt = await cursor.fetchall()
                await cursor.close()
                dt_str = dt[0][0]
                while len(dt_str.split("-")[0]) < 4:
                    dt_str = "0" + dt_str
                if (datetime.datetime.utcnow() - datetime.datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')).total_seconds() > 60 * 60:
                    await db.execute("UPDATE YoutubeChannels SET LastLive=? WHERE ID=?", (datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), obj["feed"]["entry"]["yt:channelId"]))
                    await db.commit()
                else:
                    # stream was restarted
                    return web.Response()
            else:
                # youtube does not tell if the notification is about a new video
                # or edits to an old one
                # so this checks if it's a new video or just an edit
                cursor = await db.execute("SELECT LastVideoID, VideoCount FROM YoutubeChannels WHERE ID=?", (obj["feed"]["entry"]["yt:channelId"],))
                r = await cursor.fetchall()
                stats = r[0]
                await cursor.close()
                if obj["feed"]["entry"]["yt:videoId"] != stats[0] and int(channel_obj["statistics"]["videoCount"]) > stats[1]:
                    await db.execute("UPDATE YoutubeChannels SET LastVideoID=?, VideoCount=? WHERE ID=?",
                                     (obj["feed"]["entry"]["yt:videoId"], int(channel_obj["statistics"]["videoCount"]), obj["feed"]["entry"]["yt:channelId"]))
                    await db.commit()
                else:
                    # A video has been edited
                    return web.Response()

            # send messages in all subscribed servers
            cursor = await db.execute("SELECT Guilds.YoutubeNotifChannel, YoutubeSubscriptions.OnlyStreams \
                                       FROM YoutubeSubscriptions INNER JOIN Guilds \
                                       ON YoutubeSubscriptions.Guild=Guilds.ID \
                                       WHERE YoutubeChannel=?", (obj["feed"]["entry"]["yt:channelId"],))

            async for row in cursor:
                # if the server set the subscription to "Only streams"
                # videos will not be announced
                if row[1] == 1 and video["liveBroadcastContent"] == "none":
                    continue
                announceChannel = self.bot.get_channel(row[0])
                await announceChannel.send(announcement, embed=emb)

            await cursor.close()
        return web.Response()

    # handler for posts to the /twitch route
    async def twitch(self, request):
        obj = await request.json()
        # check if it's a "stream down" notification, it does not contain any content
        if len(obj["data"]) == 0:
            # stream down
            return web.Response()

        data = obj["data"][0]

        # getting channel data
        parsingChannelUrl = "https://api.twitch.tv/helix/users"
        parsingChannelHeader = {'Client-ID': auth_token.twitch}
        parsingChannelQueryString = {"id": data["user_id"]}
        async with self.bot.session.get(parsingChannelUrl, headers=parsingChannelHeader, params=parsingChannelQueryString) as resp:
            channel_obj = await resp.json()
        ch = channel_obj["data"][0]

        # getting game data
        parsingChannelUrl = "https://api.twitch.tv/helix/games"
        parsingChannelHeader = {'Client-ID': auth_token.twitch}
        parsingChannelQueryString = {"id": data["game_id"]}
        async with self.bot.session.get(parsingChannelUrl, headers=parsingChannelHeader, params=parsingChannelQueryString) as resp:
            game_obj = await resp.json()

        # variables for game information
        # if no game is specified a default will be chosen
        if len(game_obj["data"]) == 0:
            game_url = ""
            game_name = "a game"
        else:
            ga = game_obj["data"][0]
            game_url = ga["box_art_url"].format(width=300, height=300)
            game_name = ga["name"]

        # creation of the message embed
        emb = discord.Embed(title=data["title"],
                            description=ch["display_name"],
                            url="https://www.twitch.tv/" + ch["login"],
                            color=discord.Colour.purple())
        emb.timestamp = datetime.datetime.utcnow()
        emb.set_image(url=data["thumbnail_url"].format(width=320, height=180))
        emb.set_footer(icon_url=ch["profile_image_url"], text="Twitch")
        emb.set_thumbnail(url=game_url)

        async with aiosqlite.connect("data.db") as db:
            # streams should only be announced every hour
            # to keep channels from getting spammed with stream restarts
            cursor = await db.execute("SELECT LastLive FROM TwitchChannels WHERE ID=?", (ch["id"],))
            dt = await cursor.fetchall()
            await cursor.close()
            dt_str = dt[0][0]
            while len(dt_str.split("-")[0]) < 4:
                dt_str = "0" + dt_str
            if (datetime.datetime.utcnow() - datetime.datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')).total_seconds() > 60 * 60:
                await db.execute("UPDATE TwitchChannels SET LastLive=? WHERE ID=?", (datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), ch["id"]))
                await db.commit()
            else:
                # stream was restarted
                return web.Response()

            # sending messages to all subscribed servers
            cursor = await db.execute("SELECT Guilds.TwitchNotifChannel \
                                       FROM TwitchSubscriptions INNER JOIN Guilds \
                                       ON TwitchSubscriptions.Guild=Guilds.ID \
                                       WHERE TwitchChannel=?", (data["user_id"],))

            async for row in cursor:
                announceChannel = self.bot.get_channel(row[0])
                await announceChannel.send(ch["display_name"] + " is now live with " + game_name + " !", embed=emb)

            await cursor.close()
        return web.Response()

    # handler for posts to the /surrenderat20 endpoint
    async def surrenderat20(self, request):
        obj = await request.json()
        item = obj["items"][0]

        emb = discord.Embed(title=item["title"],
                            color=discord.Colour.orange(),
                            description=" ".join(item["categories"]),
                            url=item["permalinkUrl"],
                            timestamp=datetime.datetime.utcnow())
        emb.set_thumbnail(url="https://images-ext-2.discordapp.net/external/p4GLboECWMVLnDH-Orv6nkWm3OG8uLdI2reNRQ9RX74/http/3.bp.blogspot.com/-M_ecJWWc5CE/Uizpk6U3lwI/AAAAAAAACLo/xyh6eQNRzzs/s640/sitethumb.jpg")
        if item["actor"]["id"] == "Aznbeat":
            author_img = "https://images-ext-2.discordapp.net/external/HI8rRYejC0QYULMmoDBTcZgJ52U0Msvwj9JmUxd-JAI/https/disqus.com/api/users/avatars/Aznbeat.jpg"
        else:
            author_img = "https://images-ext-2.discordapp.net/external/t0bRQzNtKHoIDcFcj2X8R0O0UPqeeyKdvawNbVMoHXE/https/disqus.com/api/users/avatars/Moobeat.jpg"
        emb.set_author(name=item["actor"]["displayName"], icon_url=author_img)

        try:
            content = item["content"]
        except KeyError:
            parsingChannelUrl = "https://www.googleapis.com/blogger/v3/blogs/8141971962311514602/posts/" + item["id"][-19:]
            parsingChannelQueryString = {"key": auth_token.google, "fields": "content"}
            async with self.bot.session.get(parsingChannelUrl, params=parsingChannelQueryString) as resp:
                post_obj = await resp.json()
            content = post_obj["content"]

        startImgPos = content.find('<img', 0, len(content)) + 4
        if(startImgPos > -1):
            endImgPos = content.find('>', startImgPos, len(content))
            imageTag = content[startImgPos:endImgPos]
            startSrcPos = imageTag.find('src="', 0, len(content)) + 5
            endSrcPos = imageTag.find('"', startSrcPos, len(content))
            linkTag = imageTag[startSrcPos:endSrcPos]

            emb.set_image(url=linkTag)

        async with aiosqlite.connect("data.db") as db:
            subscriptions = await db.execute("SELECT * FROM SurrenderAt20Subscriptions")
            async for guild_subscriptions in subscriptions:
                for category in item["categories"]:
                    if category == "Red Posts":
                        if guild_subscriptions[1] == 1:
                            break
                    elif category == "PBE":
                        if guild_subscriptions[2] == 1:
                            break
                    elif category == "Rotations":
                        if guild_subscriptions[3] == 1:
                            break
                    elif category == "Esports":
                        if guild_subscriptions[4] == 1:
                            break
                    elif category == "Releases":
                        if guild_subscriptions[5] == 1:
                            break
                else:
                    continue

                guild_emb = emb
                keywords = await db.execute("SELECT Keyword FROM Keywords WHERE Guild=?", (guild_subscriptions[0],))
                async for keyword in keywords:
                    kw = " " + keyword[0] + " "
                    # check if keyword appears in post
                    brokentext = content.replace("<br />", "\n")
                    cleantext = re.sub(self.cleanr, '', brokentext).replace("&nbsp;", " ")
                    if kw in cleantext.lower():
                        extracts = []
                        # find paragraphs with keyword
                        for part in cleantext.split("\n\n"):
                            if kw in part.lower():
                                extracts.append(part.strip())

                        # create message embed and send it to the server
                        exctrats_string = "\n\n".join(extracts)
                        if len(exctrats_string) > 950:
                            exctrats_string = exctrats_string[:950] + "... `" + str(cleantext.lower().count(kw)) + "` mentions in total"

                        guild_emb.add_field(name=f"'{keyword[0]}' was mentioned in this post!", value=exctrats_string, inline=False)
                await keywords.close()

                channels = await db.execute("SELECT SurrenderAt20NotifChannel FROM Guilds WHERE ID=?", (guild_subscriptions[0],))
                channel_id = await channels.fetchone()
                await channels.close()
                channel = self.bot.get_channel(channel_id[0])
                await channel.send("New Surrender@20 post!", embed=guild_emb)

        return web.Response()

    # various verification endpoints
    # will verify the url as my own to google
    async def googleverification(self, request):
        return web.Response(text="google-site-verification: " + auth_token.google_callback_verification)

    # will verify youtube subscriptions
    async def youtubeverification(self, request):
        return web.Response(text=request.query["hub.challenge"])

    # will verify twitch subscriptions
    async def twitchverification(self, request):
        return web.Response(text=request.query["hub.challenge"])

    # will verify surrenderat20 subscription
    async def surrenderat20verification(self, request):
        return web.Response(text=request.query["hub.challenge"])


def setup(bot):
    bot.add_cog(Webserver(bot))
