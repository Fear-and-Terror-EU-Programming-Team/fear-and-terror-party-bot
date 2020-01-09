#!/usr/bin/env python3
'''
Fear and Terror's bot for party matchmaking on Discord
'''
import asyncio
import config
import discord
import party
import re
import sys
from channelinformation import PartyChannelInformation, GamesChannelInformation
from party import Party
from database import Database
from discord.ext import commands
from emojis import Emojis
from strings import Strings
from synchronization import synchronized
from timers import channel_time_protection, message_delayed_delete

bot = commands.Bot(command_prefix=config.BOT_CMD_PREFIX)


###############################################################################
## Events
###############################################################################
@bot.event
async def on_ready():
    print('Logged in as')
    print(bot.user.name)
    print(bot.user.id)
    print('------')


@bot.event
async def on_message(message):
    await process_role_message(message)
    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload):
    await handle_react(payload, True)


@bot.event
async def on_raw_reaction_remove(payload):
    await handle_react(payload, False)

@bot.event
async def on_raw_message_edit(payload):
    # TODO change to following once discord.py 1.3.0 is released
    #message = await bot.get_channel(payload.channel_id) \
    #    .fetch_message(payload.message_id)
    #if str(payload.channel_id) not in db.games_channels():
    #    return # ignore message outside of games channels

    db = Database.load()
    message = None
    for c_id in db.games_channels().keys():
        c = bot.get_channel(int(c_id))
        try:
            message = await c.fetch_message(payload.message_id)
            break
        except discord.errors.NotFound as e:
            pass

    if message is None:
        return # message not in active games channel

    await message.clear_reactions()
    await process_role_message(message)


@synchronized  # users will break this if it's not done in sequential order
async def handle_react(payload, was_added):
    # ignore reaction if message was already deleted (synchronization stuff)
    try:
        await bot.get_channel(payload.channel_id) \
                .fetch_message(payload.message_id)
    except discord.NotFound as e:
        return

    rp = await unwrap_payload(payload)

    if rp.member == rp.guild.me:  # ignore bot reactions
        return

    db = Database.load()
    if str(rp.channel.id) in db.party_channels():
        await handle_react_party_channel(rp, was_added)
    if str(rp.channel.id) in db.games_channels():
        await handle_react_games_channel(rp, was_added)

    # ignore otherwise


async def handle_react_party_channel(rp, was_added):
    if rp.message.author != rp.guild.me:  # ignore reactions on non-bot messages
        return
    # ignore reactions on messages other than the party message
    # (identified by having exactly one embed)
    if len(rp.message.embeds) != 1:
        return
    if rp.emoji.name in emoji_handlers.keys():
        add_handler, remove_handler = emoji_handlers[rp.emoji.name]
        if was_added and add_handler is not None:
            await add_handler(rp)
        elif remove_handler is not None:
            await remove_handler(rp)


async def handle_react_games_channel(rp, was_added):
    if not was_added:
        return

    await rp.message.remove_reaction(rp.emoji, rp.member)

    game_name = translate_emoji_game_name(rp.message, rp.emoji)
    if game_name is None:
        return # ignore


    db = Database.load()
    channel_info = db.games_channels()[str(rp.channel.id)]
    # check if user already created a party channel
    vc_id = channel_info.channel_owners.get(str(rp.member.id))
    if vc_id is not None:
        vc_id = int(vc_id)
        # make sure it's actually still there
        if rp.guild.get_channel(vc_id) is None:
            print(f"VC deletion was not tracked!\n"
                  f"- Owner: {rp.member}\n", file=sys.stderr)
            del channel_info.channel_owners[str(rp.member.id)]
        else:
            message = await rp.channel.send(f"{rp.member.mention} "
                                            f"You already have an open channel.")
            asyncio.ensure_future(message_delayed_delete(message))
            return

    if game_name not in channel_info.counters:
        channel_info.counters[game_name] = 0
    channel_info.counters[game_name] += 1
    counter = channel_info.counters[game_name]
    channel_below, channel_below_position = \
            await channel_info.fetch_channel_below(rp.guild)
    category = rp.guild.get_channel(channel_below.category_id)

    vc = await rp.guild.create_voice_channel(f"{game_name} - #{counter}",
                                             category=category)
    await vc.edit(position=channel_below_position + 0)
    channel_info.channel_owners.update({str(rp.member.id): str(vc.id)})
    db.save()
    asyncio.ensure_future(channel_time_protection(vc, callback=lambda vc:\
                            games_channel_deletion_callback(rp.channel, vc)))

    message = await rp.channel.send(f"{rp.member.mention} "
                                    f"Connect to {vc.mention}.")
    asyncio.ensure_future(message_delayed_delete(message))


def games_channel_deletion_callback(games_channel, voice_channel):
    db = Database.load()
    channel_info = db.games_channels()[str(games_channel.id)]
    del channel_info.channel_owners.inverse[str(voice_channel.id)]
    db.save()


# handle emoji reactions being added deleted/
# Format:
#   Emoji : (add_handler, remove_handler)
# All handlers are expected to take exactly one argument: the ReactionPayload
emoji_handlers = {
    Emojis.WHITE_CHECK_MARK:
        (party.add_member_emoji_handler, party.remove_member_emoji_handler),
    Emojis.FAST_FORWARD:
        (party.force_start_party, None),
    Emojis.NO_ENTRY_SIGN:
        (party.close_party, None),
    Emojis.TADA:
        (party.start_party, None)
}


@bot.event
async def on_voice_state_update(member, before, after):
    channel = before.channel
    if channel is None \
            or after.channel == channel:  # only tracks disconnects
        return
    if len(channel.members) > 0:  # only react on empty channels
        return

    # only track channels created by the party bot
    db = Database.load()
    mm_channel_id = None
    for cur_mm_channel_id, cur_mm_channel_info in db.party_channels().items():
        if channel.id in cur_mm_channel_info.active_voice_channels:
            mm_channel_id = cur_mm_channel_id
            break
    if mm_channel_id == None:
        return

    await party.handle_party_emptied(mm_channel_id, channel)


class ReactionPayload():
    # this might be a bit heavy on the API
    async def _init(self, payload):
        self.guild = bot.get_guild(payload.guild_id)
        self.member = await self.guild.fetch_member(payload.user_id)
        self.emoji = payload.emoji
        self.channel = bot.get_channel(payload.channel_id)
        self.message = await self.channel.fetch_message(payload.message_id)


async def unwrap_payload(payload):
    rp = ReactionPayload()
    await rp._init(payload)
    return rp


def is_admin():
    async def predicate(ctx):
        return party.is_admin(ctx.author)
    return commands.check(predicate)


async def process_role_message(message):
    if not party.is_admin(message.author):
        return # ignore non-admin message
    if message.author is bot.user:
        return # ignore bot messages
    db = Database.load()
    if not str(message.channel.id) in db.games_channels():
        return # ignore messages in non-games channels

    translations = get_emoji_game_name_translations(message)
    for emoji in translations.keys():
        await message.add_reaction(emoji) # custom emojis?


def get_emoji_game_name_translations(message):
    translations = {}
    pattern = r">* *([^ \n]+) ([^\n]+)"
    for match in re.finditer(pattern, message.content):
        expected_emoji, game_name = match.group(1,2)
        translations[expected_emoji] = game_name
    return translations


def translate_emoji_game_name(message, emoji):
    emoji = str(emoji)

    # get all emoji-to-role translations by parsing the message
    translations = get_emoji_game_name_translations(message)
    return translations.get(emoji)

###############################################################################
## Commands
###############################################################################


@bot.command()
@is_admin()
async def activatechannel(ctx, game_name: str,
                          max_slots: int, channel_above_id: int,
                          open_parties : str):
    if open_parties == Strings.OPEN_PARTIES:
        open_parties = True
    elif open_parties == Strings.CLOSED_PARTIES:
        open_parties = False
    else:
        raise commands.errors.BadArgument()

    channel_above = ctx.guild.get_channel(channel_above_id)
    if channel_above is None:
        raise commands.errors.BadArgument()

    db = Database.load()
    if ctx.channel.id not in db.party_channels():
        await ctx.message.delete()
        await ctx.send(f"This channel has been activated for party matchmaking. ")

    else:
        await ctx.message.delete()
        await ctx.send(f"Channel configuration updated.")

    channel_info = PartyChannelInformation(game_name, ctx.channel, max_slots,
                                           channel_above, open_parties)

    db.party_channels()[str(ctx.channel.id)] = channel_info
    db.save()
    await ctx.channel.purge(limit=100, check=is_me)
    embed = discord.Embed.from_dict({
        "title": "Game: %s" % game_name,
        "color": 0x0000FF,
        "description": "React with %s to start a party for %s." \
                       % (Emojis.TADA, game_name)
    })
    message = await ctx.send("", embed=embed)
    await message.add_reaction(Emojis.TADA)


@activatechannel.error
async def activatechannel_error(ctx, error):
    error_handlers = get_default_error_handlers(ctx, "activatechannel",
                                                f"GAME_NAME "
                                                f"MAX_SLOTS CHANNEL_ABOVE_ID "
                                                f"({Strings.OPEN_PARTIES}|"
                                                f"{Strings.CLOSED_PARTIES})")
    await handle_error(ctx, error, error_handlers)


def is_me(m):
    return m.author == bot.user

@bot.command()
@is_admin()
async def deactivatechannel(ctx):
    check_channel(ctx.channel)
    db = Database.load()
    del db.party_channels()[str(ctx.channel.id)]
    db.save()
    await ctx.message.delete()
    await ctx.channel.purge(limit=100, check=is_me)
    message = await ctx.send(f"Party matchmaking disabled for this channel.")
    asyncio.ensure_future(message_delayed_delete(message))


@deactivatechannel.error
async def deactivatechannel_error(ctx, error):
    error_handlers = get_default_error_handlers(ctx, "deactivate", "")
    await handle_error(ctx, error, error_handlers)


# @bot.command()
# @is_admin()
# async def nukeparties(ctx):
#    for channel in ctx.guild.channels:
#        if " Party #" in channel.name:
#            await channel.delete()

@bot.command()
@is_admin()
async def activategameschannel(ctx, channel_below_id: int):
    channel_below = ctx.guild.get_channel(channel_below_id)
    if channel_below is None:
        raise commands.errors.BadArgument()

    db = Database.load()
    games_channels = db.games_channels()
    if ctx.channel.id in games_channels:
        raise ChannelAlreadyActiveError()

    await ctx.message.delete()
    message = await ctx.send(f"Channel activated for side-game party creation.")
    asyncio.ensure_future(message_delayed_delete(message))

    channel_info = GamesChannelInformation(ctx.channel, channel_below)
    games_channels[str(ctx.channel.id)] = channel_info
    db.save()


@activategameschannel.error
async def activategameschannel_error(ctx, error):
    error_handlers = get_default_error_handlers(ctx, "activategameschannel",
                                                "CHANNEL_BELOW_ID")
    error_handlers.update({
        ChannelAlreadyActiveError: lambda:
        ctx.send("Channel already activated."),
    })
    await handle_error(ctx, error, error_handlers)


@bot.command()
@is_admin()
async def deactivategameschannel(ctx):
    db = Database.load()
    games_channels = db.games_channels()
    if str(ctx.channel.id) not in games_channels:
        raise InactiveChannelError()

    await ctx.message.delete()
    message = await ctx.send(f"Channel deactivated for side-game party creation.")
    asyncio.ensure_future(message_delayed_delete(message))

    del games_channels[str(ctx.channel.id)]
    db.save()


@deactivategameschannel.error
async def deactivategameschannel_error(ctx, error):
    error_handlers = get_default_error_handlers(ctx, "deactivategameschannel")
    error_handlers.update({
        InactiveChannelError: lambda:
        ctx.send(f"The bot is not configured to use this channel.")
    })
    await handle_error(ctx, error, error_handlers)

###############################################################################
## Command error handling
###############################################################################

class ChannelAlreadyActiveError(commands.CommandError): pass


class InactiveChannelError(commands.CommandError): pass


class PartyAlreadyStartedError(commands.CommandError): pass


class NoActivePartyError(commands.CommandError): pass


async def handle_error(ctx, error, error_handlers):
    for error_type, handler in error_handlers.items():
        if isinstance(error, error_type):
            await handler()
            return

    await send_error_unknown(ctx)
    raise error


def get_default_error_handlers(ctx, command_name, command_argument_syntax=""):
    '''Generate default error handlers including ones for bad argument syntax
    and invalid channel.
    '''
    usage_help = lambda: send_usage_help(ctx, command_name,
                                         command_argument_syntax)
    return {
        commands.errors.MissingRequiredArgument: usage_help,
        commands.errors.BadArgument: usage_help,
        commands.MissingRole: lambda:
        ctx.send("Insufficient rank permissions."),
        commands.errors.CheckFailure: lambda:
        ctx.send("Insufficient rank permissions."),
        InactiveChannelError: lambda:
        ctx.send(f"The bot is not configured to use this channel. "
                 f"Admins can change that via "
                 f"{config.BOT_CMD_PREFIX}activatechannel.")
    }


def send_usage_help(ctx, function_name, argument_structure):
    return ctx.send(f"Usage: `{config.BOT_CMD_PREFIX}{function_name} "
                    f"{argument_structure}`")


def send_error_unknown(ctx):
    return send_error(ctx, f"Unknown error. Tell someone from the programming"
                           f" team to check the logs.")


def send_error(ctx, text):
    return ctx.send("[ERROR] " + text)


def check_channel(channel):
    '''Raises an InactiveChannelError if the channel is not marked as active.'''
    db = Database.load()
    if str(channel.id) not in db.party_channels():
        raise InactiveChannelError()


###############################################################################
## Startup
###############################################################################

if __name__ == "__main__":
    bot.run(config.BOT_TOKEN)
