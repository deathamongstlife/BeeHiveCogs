import discord
import discord.ui
from red_commons.logging import getLogger
from redbot.core import commands, Config, checks, app_commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, pagify

log = getLogger("red.beehive.compliance")

class ComplianceManager(commands.Cog):
    """
    Compliance Manager for Guilds

    Enforce and manage requirements for which guilds your bot is allowed to be in.
    """

    __version__ = "1.0.0"
    __author__ = "BeeHive"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=111111111111111, force_registration=True)
        default_global = {
            "allowed_guilds": [],
            "blocked_guilds": [],
            "blocked_guild_reasons": {},  # {guild_id: reason}
            "min_member_count": 0,
            "requirements_enabled": False,
            "enforcement_interval": 3600,  # seconds
            "log_channel": None,
            "max_guilds": 0,  # 0 means unlimited
        }
        self.config.register_global(**default_global)
        self._enforcement_task = bot.loop.create_task(self._enforce_loop())
        bot.add_listener(self._on_guild_join, "on_guild_join")

    def cog_unload(self):
        self._enforcement_task.cancel()
        self.bot.remove_listener(self._on_guild_join, "on_guild_join")

    async def _on_guild_join(self, guild: discord.Guild):
        """
        When the bot joins a guild, check if it's on the blocklist or over max_guilds.
        If so, DM the inviter and leave the guild.
        """
        blocked = await self.config.blocked_guilds()
        max_guilds = await self.config.max_guilds()
        # Check if over max_guilds (if set and not 0)
        if max_guilds and len(self.bot.guilds) > max_guilds:
            inviter = None
            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                    if entry.target.id == self.bot.user.id:
                        inviter = entry.user
                        break
            except Exception as e:
                log.debug(f"Could not fetch audit log for guild {guild.id}: {e}")
            if not inviter:
                inviter = guild.owner
            if inviter:
                try:
                    embed = discord.Embed(
                        title="There's currently a waitlist",
                        description=(
                            f"Hello! Thank you for inviting me to **{guild.name}**.\n\n"
                            f"Unfortunately, I am currently at my maximum allowed number of servers (`{max_guilds}`) and cannot remain in this server."
                        ),
                        color=0xff4545
                    )
                    embed.set_footer(text="If you believe this is a mistake, please contact the bot owner.")
                    await inviter.send(embed=embed)
                except Exception as e:
                    log.debug(f"Could not DM inviter/owner ({inviter}) for max guilds {guild.id}: {e}")
            try:
                await guild.leave()
                log.info(f"Left guild {guild.name} ({guild.id}) on join due to max_guilds limit.")
            except Exception as e:
                log.error(f"Failed to leave guild {guild.name} ({guild.id}) due to max_guilds: {e}")
            return

        if guild.id in blocked:
            inviter = None
            # Try to get the inviter from the audit log (if permissions allow)
            try:
                # Only works if bot has 'View Audit Log' permission
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                    if entry.target.id == self.bot.user.id:
                        inviter = entry.user
                        break
            except Exception as e:
                log.debug(f"Could not fetch audit log for guild {guild.id}: {e}")

            # Fallback: try to DM the owner if inviter not found
            if not inviter:
                inviter = guild.owner

            # Attempt to DM the inviter/owner
            if inviter:
                try:
                    # Try to include the block reason if available
                    blocked_reasons = await self.config.blocked_guild_reasons()
                    reason = blocked_reasons.get(str(guild.id))
                    embed = discord.Embed(
                        title="Compliance Block Notice",
                        description=(
                            f"Hello! Thank you for inviting me to **{guild.name}**.\n\n"
                            f"Unfortunately, this server is currently on the compliance blocklist and I am unable to remain here or provide any features."
                        ),
                        color=0xff4545
                    )
                    if reason:
                        embed.add_field(name="Blocked for", value=reason, inline=False)
                    embed.set_footer(text="If you believe this is a mistake, please contact the bot owner.")
                    await inviter.send(embed=embed)
                except Exception as e:
                    log.debug(f"Could not DM inviter/owner ({inviter}) for blocked guild {guild.id}: {e}")

            # Leave the guild
            try:
                await guild.leave()
                log.info(f"Left blocked guild {guild.name} ({guild.id}) on join.")
            except Exception as e:
                log.error(f"Failed to leave blocked guild {guild.name} ({guild.id}): {e}")

    async def _enforce_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.enforce_compliance()
            except Exception as e:
                log.exception("Error during compliance enforcement: %s", e)
            interval = await self.config.enforcement_interval()
            await discord.utils.sleep_until(discord.utils.utcnow() + discord.utils.timedelta(seconds=interval))

    async def enforce_compliance(self):
        enabled = await self.config.requirements_enabled()
        if not enabled:
            return
        allowed = await self.config.allowed_guilds()
        blocked = await self.config.blocked_guilds()
        min_members = await self.config.min_member_count()
        log_channel_id = await self.config.log_channel()
        max_guilds = await self.config.max_guilds()
        log_channel = None
        if log_channel_id:
            log_channel = self.bot.get_channel(log_channel_id)
        left_guilds = []
        # 1. Enforce blocklist, allowlist, min_members as before
        for guild in self.bot.guilds:
            if guild.id in blocked:
                await guild.leave()
                left_guilds.append((guild, "blocked"))
                continue
            if allowed and guild.id not in allowed:
                await guild.leave()
                left_guilds.append((guild, "not allowed"))
                continue
            if min_members and guild.member_count < min_members:
                await guild.leave()
                left_guilds.append((guild, "too small"))
        # 2. Enforce max_guilds (if set and not 0)
        if max_guilds and len(self.bot.guilds) > max_guilds:
            # Sort by join date (oldest first), so we leave the most recently joined
            sorted_guilds = sorted(self.bot.guilds, key=lambda g: g.me.joined_at or 0, reverse=False)
            # Keep the first max_guilds, leave the rest
            to_leave = sorted_guilds[max_guilds:]
            for guild in to_leave:
                try:
                    await guild.leave()
                    left_guilds.append((guild, "over max_guilds"))
                except Exception as e:
                    log.error(f"Failed to leave guild {guild.name} ({guild.id}) due to max_guilds: {e}")
        if left_guilds and log_channel:
            embed = discord.Embed(
                title="Compliance enforcement",
                color=discord.Color.orange(),
                description="The bot has left the following guilds due to compliance enforcement."
            )
            for g, reason in left_guilds:
                embed.add_field(
                    name=f"{g.name} ({g.id})",
                    value=f"Reason: `{reason}`",
                    inline=False
                )
            await log_channel.send(embed=embed)

    @commands.group(name="compliance", invoke_without_command=True)
    async def compliance(self, ctx):
        """Compliance manager for guilds.

        If you are not a bot owner, this will show your server's compliance status.
        """
        # Check if the user is a bot owner
        is_owner = await self.bot.is_owner(ctx.author)
        if is_owner:
            await ctx.send_help()
            return

        # Not a bot owner: show this server's compliance status
        guild = ctx.guild
        if not guild:
            await ctx.send("This command must be used in a server.")
            return

        enabled = await self.config.requirements_enabled()
        allowed = await self.config.allowed_guilds()
        blocked = await self.config.blocked_guilds()
        blocked_reasons = await self.config.blocked_guild_reasons()
        min_members = await self.config.min_member_count()
        max_guilds = await self.config.max_guilds()

        # Determine compliance status for this guild
        guild_status = []
        if guild.id in blocked:
            reason = blocked_reasons.get(str(guild.id))
            guild_status.append("❌ **Blocked**")
            if reason:
                guild_status.append(f"**Reason:** {reason}")
        elif allowed and guild.id not in allowed:
            guild_status.append("❌ **Not on allowlist**")
        elif min_members and guild.member_count < min_members:
            guild_status.append(f"❌ **Too few members** ({guild.member_count} < {min_members})")
        elif enabled:
            guild_status.append("✅ **Compliant**")
        else:
            guild_status.append("Compliance enforcement is currently **disabled**.")

        embed = discord.Embed(
            title=f"Compliance Status for {guild.name}",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="Status",
            value="\n".join(guild_status),
            inline=False
        )
        embed.add_field(
            name="Compliance Enforcement",
            value="Enabled" if enabled else "Disabled",
            inline=True
        )
        embed.add_field(
            name="Min Member Count",
            value=f"{min_members}",
            inline=True
        )
        embed.add_field(
            name="Max Guilds",
            value=f"{max_guilds}" if max_guilds else "Unlimited",
            inline=True
        )
        if allowed:
            embed.add_field(
                name="Allowlist",
                value="This server is on the allowlist." if guild.id in allowed else "This server is **not** on the allowlist.",
                inline=False
            )
        if guild.id in blocked:
            embed.add_field(
                name="Blocklist",
                value="This server is on the blocklist.",
                inline=False
            )
        await ctx.send(embed=embed)

    @compliance.command(name="enable")
    @checks.is_owner()
    async def compliance_enable(self, ctx):
        """Enable compliance enforcement."""
        await self.config.requirements_enabled.set(True)
        embed = discord.Embed(
            title="Compliance Enforcement Enabled",
            description="✅ Compliance enforcement has been enabled.",
            color=0x2bbd8e
        )
        await ctx.send(embed=embed)

    @compliance.command(name="disable")
    @checks.is_owner()
    async def compliance_disable(self, ctx):
        """Disable compliance enforcement."""
        await self.config.requirements_enabled.set(False)
        embed = discord.Embed(
            title="Compliance Enforcement Disabled",
            description="❌ Compliance enforcement has been disabled.",
            color=0xff4545
        )
        await ctx.send(embed=embed)

    @compliance.command(name="allow")
    @checks.is_owner()
    async def compliance_toggle_allowed(self, ctx, guild_id: int):
        """
        Toggle a guild in the allowed list.
        If the guild is in the allowed list, it will be removed.
        If not, it will be added.
        """
        allowed = await self.config.allowed_guilds()
        if guild_id in allowed:
            allowed.remove(guild_id)
            await self.config.allowed_guilds.set(allowed)
            embed = discord.Embed(
                title="Guild Removed from Allowed List",
                description=f"❌ Guild `{guild_id}` removed from allowed list.",
                color=0xff4545
            )
            await ctx.send(embed=embed)
        else:
            allowed.append(guild_id)
            await self.config.allowed_guilds.set(allowed)
            embed = discord.Embed(
                title="Guild Added to Allowed List",
                description=f"✅ Guild `{guild_id}` added to allowed list.",
                color=0x2bbd8e
            )
            await ctx.send(embed=embed)

    @compliance.command(name="block")
    @checks.is_owner()
    async def compliance_toggle_blocked(self, ctx, guild_id: int, *, reason: str = None):
        """
        Toggle a guild in the blocked list.
        If the guild is in the blocked list, it will be removed (and its reason deleted).
        If not, it will be added, and you may provide a reason (optional).
        Example: [p]compliance block 1234567890 Spamming
        """
        blocked = await self.config.blocked_guilds()
        blocked_reasons = await self.config.blocked_guild_reasons()
        guild_id_str = str(guild_id)
        if guild_id in blocked:
            blocked.remove(guild_id)
            await self.config.blocked_guilds.set(blocked)
            embed = discord.Embed(
                title="Guild compliance block removed",
                description=f"❌ Guild `{guild_id}` removed from blocked list.",
                color=0xff4545
            )
            if guild_id_str in blocked_reasons:
                del blocked_reasons[guild_id_str]
                await self.config.blocked_guild_reasons.set(blocked_reasons)
                embed.add_field(
                    name="Reason removed",
                    value="Any block reason for this guild has been deleted.",
                    inline=False
                )
            await ctx.send(embed=embed)
        else:
            blocked.append(guild_id)
            await self.config.blocked_guilds.set(blocked)
            embed = discord.Embed(
                title="Guild compliance block added",
                description=f"Guild `{guild_id}` is now subject to a compliance block.",
                color=0x2bbd8e
            )
            if reason:
                blocked_reasons[guild_id_str] = reason
                await self.config.blocked_guild_reasons.set(blocked_reasons)
                embed.add_field(
                    name="Blocked for",
                    value=reason,
                    inline=False
                )
            else:
                embed.add_field(
                    name="Blocked for",
                    value="No reason provided.",
                    inline=False
                )
            await ctx.send(embed=embed)

    @compliance.command(name="minimum")
    @checks.is_owner()
    async def compliance_min_members(self, ctx, count: int):
        """Set a guild minimum member count"""
        await self.config.min_member_count.set(count)
        embed = discord.Embed(
            title="Minimum Member Count Set",
            description=f"✅ Minimum member count set to `{count}`.",
            color=0x2bbd8e
        )
        await ctx.send(embed=embed)

    @compliance.command(name="maxguilds")
    @checks.is_owner()
    async def compliance_set_max_guilds(self, ctx, count: int):
        """
        Set the maximum number of servers the bot can be in at once.
        Set to 0 for unlimited.
        """
        await self.config.max_guilds.set(count)
        if count == 0:
            desc = "✅ Maximum guilds limit removed (unlimited)."
            color = discord.Color.orange()
        else:
            desc = f"✅ Maximum guilds set to `{count}`."
            color = 0x2bbd8e
        embed = discord.Embed(
            title="Maximum Guilds Set",
            description=desc,
            color=color
        )
        await ctx.send(embed=embed)

    @compliance.command(name="interval")
    @checks.is_owner()
    async def compliance_set_interval(self, ctx, seconds: int):
        """Set the enforcement interval in seconds."""
        await self.config.enforcement_interval.set(seconds)
        embed = discord.Embed(
            title="Enforcement Interval Set",
            description=f"✅ Enforcement interval set to `{seconds}` seconds.",
            color=0x2bbd8e
        )
        await ctx.send(embed=embed)

    @compliance.command(name="logs")
    @checks.is_owner()
    async def compliance_set_logchannel(self, ctx, channel: discord.TextChannel = None):
        """Set a compliance log channel"""
        if channel:
            await self.config.log_channel.set(channel.id)
            embed = discord.Embed(
                title="Log Channel Set",
                description=f"✅ Log channel set to {channel.mention}.",
                color=0x2bbd8e
            )
            await ctx.send(embed=embed)
        else:
            await self.config.log_channel.set(None)
            embed = discord.Embed(
                title="Log Channel Cleared",
                description="✅ Log channel cleared.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)

    @compliance.command(name="status")
    @checks.is_owner()
    async def compliance_status(self, ctx):
        """Show current compliance settings and guilds."""
        enabled = await self.config.requirements_enabled()
        allowed = await self.config.allowed_guilds()
        blocked = await self.config.blocked_guilds()
        blocked_reasons = await self.config.blocked_guild_reasons()
        min_members = await self.config.min_member_count()
        interval = await self.config.enforcement_interval()
        log_channel_id = await self.config.log_channel()
        max_guilds = await self.config.max_guilds()
        log_channel = ctx.guild.get_channel(log_channel_id) if log_channel_id else None

        # Prepare blocked guilds with reasons
        if blocked:
            blocked_lines = []
            for gid in blocked:
                reason = blocked_reasons.get(str(gid))
                if reason:
                    blocked_lines.append(f"{gid} (Reason: {reason})")
                else:
                    blocked_lines.append(str(gid))
            blocked_str = ", ".join(blocked_lines)
        else:
            blocked_str = "None"

        embed = discord.Embed(
            title="Compliance Status",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="Enabled",
            value=f"`{enabled}`",
            inline=True
        )
        embed.add_field(
            name="Min Member Count",
            value=f"`{min_members}`",
            inline=True
        )
        embed.add_field(
            name="Enforcement Interval",
            value=f"`{interval}` seconds",
            inline=True
        )
        embed.add_field(
            name="Max Guilds",
            value=f"`{max_guilds}`" if max_guilds else "Unlimited",
            inline=True
        )
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "Not set",
            inline=True
        )
        embed.add_field(
            name="Allowed Guilds",
            value=(", ".join(str(i) for i in allowed) if allowed else "None"),
            inline=False
        )
        embed.add_field(
            name="Blocked Guilds",
            value=blocked_str,
            inline=False
        )
        await ctx.send(embed=embed)

    @compliance.command(name="guilds")
    @checks.is_owner()
    async def compliance_list_guilds(self, ctx):
        """List all guilds the bot is currently in."""
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count, reverse=True)
        lines = []
        for g in guilds:
            lines.append(f"{g.name} ({g.id}) - {g.member_count} members")
        # Paginate if needed
        max_field_length = 1024
        pages = []
        current_page = []
        current_length = 0
        for line in lines:
            if current_length + len(line) + 1 > max_field_length and current_page:
                pages.append(current_page)
                current_page = []
                current_length = 0
            current_page.append(line)
            current_length += len(line) + 1
        if current_page:
            pages.append(current_page)
        for idx, page_lines in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"Guilds the Bot is In ({len(guilds)})" + (f" (Page {idx}/{len(pages)})" if len(pages) > 1 else ""),
                color=discord.Color.blurple()
            )
            embed.add_field(
                name="Guilds",
                value="\n".join(page_lines),
                inline=False
            )
            await ctx.send(embed=embed)

    @compliance.command(name="enforce")
    @checks.is_owner()
    async def compliance_enforce_now(self, ctx):
        """Run compliance enforcement immediately."""
        await self.enforce_compliance()
        embed = discord.Embed(
            title="Compliance Enforcement Run",
            description="✅ Compliance enforcement run complete.",
            color=0x2bbd8e
        )
        await ctx.send(embed=embed)

    @compliance.command(name="guild")
    @checks.is_owner()
    async def compliance_guild_info(self, ctx, guild_id: int):
        """
        Fetch information about any server the bot is in by guild ID.
        If able, generate an invite for the server and attach it as a discord URL button.
        """
        guild = self.bot.get_guild(guild_id)
        if not guild:
            embed = discord.Embed(
                title="Guild Not Found",
                description=f"❌ I am not in a guild with ID `{guild_id}`.",
                color=0xff4545
            )
            await ctx.send(embed=embed)
            return

        owner = guild.owner
        created_at = guild.created_at.strftime("%Y-%m-%d %H:%M:%S")
        features = ", ".join(guild.features) if guild.features else "None"
        icon_url = guild.icon.url if guild.icon else None

        embed = discord.Embed(
            title=f"{guild.name} ({guild.id})",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Owner", value=f"{owner} ({owner.id})", inline=True)
        embed.add_field(name="Members", value=f"{guild.member_count}", inline=True)
        embed.add_field(name="Created", value=created_at, inline=True)
        embed.add_field(name="Region", value=getattr(guild, 'region', 'N/A'), inline=True)
        embed.add_field(name="Features", value=features, inline=False)
        embed.add_field(name="Verification Level", value=guild.verification_level.name, inline=True)
        embed.add_field(name="MFA Level", value='Enabled' if guild.mfa_level else 'Disabled', inline=True)
        embed.add_field(name="Partnered", value='Yes' if 'PARTNERED' in guild.features else 'No', inline=True)
        embed.add_field(name="Vanity URL", value=guild.vanity_url_code or 'None', inline=True)
        if icon_url:
            embed.set_thumbnail(url=icon_url)
        # Show up to 5 top roles (by position, excluding @everyone)
        roles = [r for r in guild.roles if r.name != "@everyone"]
        if roles:
            top_roles = sorted(roles, key=lambda r: r.position, reverse=True)[:5]
            embed.add_field(
                name="Top Roles",
                value=", ".join(r.mention for r in top_roles),
                inline=False
            )
        # Show all text channels the bot can read
        text_channels = [c for c in guild.text_channels if c.permissions_for(guild.me).read_messages]
        if text_channels:
            channel_list = ", ".join(f"#{c.name}" for c in text_channels)
            embed.add_field(
                name=f"Text Channels ({len(text_channels)})",
                value=channel_list if len(channel_list) < 1024 else "Too many channels to display.",
                inline=False
            )

        # Try to generate an invite and attach as a button
        invite_url = None
        try:
            # Find a text channel where the bot can create an invite
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if perms.create_instant_invite:
                    invite = await channel.create_invite(max_age=300, max_uses=1, unique=True, reason="Compliance review")
                    invite_url = invite.url
                    break
        except Exception as e:
            invite_url = None  # Could not create invite

        # If discord.py 2.x+ is available, use View/Button, else fallback to just sending the URL
        try:
            class InviteView(discord.ui.View):
                def __init__(self, url):
                    super().__init__()
                    if url:
                        self.add_item(discord.ui.Button(label="Server Invite", url=url, style=discord.ButtonStyle.link))

            view = InviteView(invite_url) if invite_url else None
            await ctx.send(embed=embed, view=view)
        except Exception:
            # Fallback: just send the invite URL as a message if unable to use View/Button
            await ctx.send(embed=embed)
            if invite_url:
                await ctx.send(f"Server Invite: {invite_url}")

    @compliance.command(name="staff")
    @checks.is_owner()
    async def compliance_guild_staff(self, ctx, guild_id: int):
        """
        Fetch all staff of a server by guild ID, based on staff-level permissions.
        Staff are members with any of: Administrator, Manage Guild, Manage Roles, Kick Members, Ban Members, Manage Channels, Manage Messages, Manage Webhooks, Manage Nicknames, Manage Emojis and Stickers.
        """
        guild = self.bot.get_guild(guild_id)
        if not guild:
            embed = discord.Embed(
                title="Guild Not Found",
                description=f"❌ I am not in a guild with ID `{guild_id}`.",
                color=0xff4545
            )
            await ctx.send(embed=embed)
            return

        # Define staff-level permissions to check
        # Expanded staff-level permissions to include additional relevant permissions.
        staff_perms = [
            "administrator",
            "manage_guild",
            "manage_roles",
            "kick_members",
            "ban_members",
            "manage_channels",
            "manage_messages",
            "manage_webhooks",
            "manage_nicknames",
            "manage_emojis_and_stickers",
            "manage_events",           # allows managing guild scheduled events
            "manage_threads",          # allows managing threads
            "moderate_members",        # allows timing out members (timeout feature)
            "view_audit_log",          # can view audit log (often staff)
            "manage_expressions",      # manage stickers (newer Discord permission)
            "manage_guild_expressions",# manage emojis and stickers (newer Discord permission)
            "deafen_members",          # can deafen members in voice
            "move_members",            # can move members between voice channels
            "mute_members",            # can mute members in voice
            "priority_speaker",        # can use priority speaker in voice (sometimes staff)
        ]

        staff_members = []
        for member in guild.members:
            if member.bot:
                continue  # Ignore bots
            perms: discord.Permissions = member.guild_permissions
            if any(getattr(perms, perm, False) for perm in staff_perms):
                staff_members.append(member)

        if not staff_members:
            embed = discord.Embed(
                title="No Staff Members Found",
                description="No staff members found with staff-level permissions in this guild.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            return

        # Prepare embed(s)
        embed_title = f"Staff Members in {guild.name} ({guild.id})"
        embed_color = discord.Color.blurple()
        max_field_length = 1024  # Discord embed field value max length

        # Build lines for each staff member
        lines = []
        for m in staff_members:
            perms = m.guild_permissions
            perms_list = [perm.replace("_", " ").title() for perm in staff_perms if getattr(perms, perm, False)]
            lines.append(f"{m.mention} (`{m.id}`)\n• " + ", ".join(perms_list))

        # Paginate lines into embed fields if needed
        pages = []
        current_page = []
        current_length = 0
        for line in lines:
            if current_length + len(line) + 1 > max_field_length and current_page:
                pages.append(current_page)
                current_page = []
                current_length = 0
            current_page.append(line)
            current_length += len(line) + 1
        if current_page:
            pages.append(current_page)

        for idx, page_lines in enumerate(pages, 1):
            embed = discord.Embed(
                title=embed_title + (f" (Page {idx}/{len(pages)})" if len(pages) > 1 else ""),
                color=embed_color,
                description="Staff are members with any of: Administrator, Manage Guild, Manage Roles, Kick Members, Ban Members, Manage Channels, Manage Messages, Manage Webhooks, Manage Nicknames, Manage Emojis and Stickers, Manage Events, Manage Threads, Moderate Members."
            )
            embed.add_field(
                name="Staff Members",
                value="\n\n".join(page_lines),
                inline=False
            )
            await ctx.send(embed=embed)

