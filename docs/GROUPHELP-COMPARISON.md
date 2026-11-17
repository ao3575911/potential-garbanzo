# Group Help comparison and next decisions

Reviewed 17 November 2026 (Australia/Perth): [official features](https://grouphelp.me/en), [plan comparison](https://grouphelp.me/en/pricing), [published command reference](https://telegra.ph/Group-Help-command-list-08-27), [user guide](https://telegra.ph/Group-Help-user-Guide-08-18), and [official English updates](https://t.me/s/enGroupHelp/15).

The command reference is dated **27 August 2020**. It is a useful legacy catalogue, not an authoritative inventory of every current in-bot setting. Current plan descriptions and update notices add categories. Proprietary blocklists, detection systems and auxiliary bots are not available to this independent implementation. No Group Help source code or data was copied.

## Implemented equivalents

| Capability | Circuit Perth implementation |
| --- | --- |
| Core moderation | `/ban`, `/unban`, `/kick`, `/mute`, `/unmute`, `/warn`, `/unwarn`, `/warnings` |
| Target information | `/check`, `/self`, `/handle`; stable observed IDs and separate unknown handles |
| Cleanup | `/del`, `/purge` (100-message bound); local audit |
| Pin management | `/pin`, `/unpin` on replied-to messages |
| Configuration | `/admin group`, `/admin channel`, `/settings`, `/set` |
| Spam protection | Flood window, link/media/channel-sender toggles, literal phrases; staff exemptions |
| Join checks | Optional arithmetic challenge; `/captchas`, `/verify`; join request decisions |
| Welcome and rules | Configurable text, goodbye text, `/rules` |
| Quiet mode | Hour-based message deletion; `/lock` and snapshot restoration |
| Custom responses | `/setcommand`, `/delcommand`, `/autoreply`, `/delreply`; one response per matched input |
| Announcements | `/schedule`, `/repeat`, `/timers`, `/canceltimer`; group or channel |
| Staff and invites | `/admins`, limited `/promote` and `/demote`; short one-use invites |
| Owner operations | CLI setup, discovery, doctor, backup, restore and scoped export; `/health`, `/audit`, `/deliveries`, `/inbox` |

These are equivalents in purpose; they do not implement every parameter, alias, permission tier or interaction of the reference bot. The uniquely requested reputation and anonymous publishing flows are implemented separately.

## Legacy reference coverage

The following command families from the published reference were considered. “Partial” means the first release has a related capability but omits the full family. Names below identify the reference bot’s commands, not additional commands promised by Circuit Perth.

| Reference commands | Coverage |
| --- | --- |
| `reload`, `ban`, `kick`, `warn`, `warns`, `mute`, `unban`, `unwarn`, `unmute` | Core actions covered; no manual reload needed for authorization. Timed warnings remain optional. |
| `multiban`, `mban`, `multikick`, `mkick`, `multimute`, `mmute`, `multiunban`, `munban`, `multiunmute`, `munmute` | Optional bulk operations. |
| `delban`, `delkick`, `delwarn`, `delmute` | Optional combined actions. Separate moderation/deletion commands exist. |
| `info`, `infopvt`, `me`, `staff`, `rules`, `chatid`, `help` | Related dossier, administrator, rules, ID and help commands covered. |
| `del`, `logdel`, `delall`, `delallgo`, `multidel`, `mdel`, `purge` | Partial: bounded cleanup and local audit, without external security helper bots. |
| `personal`, `remove`, `reply`, `unreply`, `commands` | Partial: text commands/replies; no chaining or command catalogue UI. |
| `antiflood`, `silence`, `unsilence`, `protezione`, `chimpostazioni`, `settings`, `config`, `pvtconfig` | Partial: settings/locks and Telegram permissions; no extra delegated settings roles. |
| `send`, `pin`, `npin`, `spin`, `editpin`, `delpin`, `repin`, `unpinall`, `pinned`, `link`, `geturl` | Partial: scheduled publishing, pin/unpin and bot-created invites. |
| `listroles`, `list`, `inactives`, `ammoniti`, `graphic`, `trend`, `views`, `addviews`, `top10`, `mutelist` | Partial: observed activity and member warnings. Further analytics are optional. Full member enumeration is unavailable through the Bot API. |
| `cofounder`, `uncofounder`, `admin`, `unadmin`, `mod`, `unmod`, `muter`, `unmuter`, `cleaner`, `uncleaner`, `helper`, `unhelper`, `free`, `unfree` | Partial: configured owners, Telegram admins and limited staff promotion; additional bot roles remain optional. |
| `block`, `unblock`, `magic`, `unmagic`, `lotteria`, `slot`, `game`, `lang`, `translate`, `trad` | Optional media-specific tools, games and translations. |
| `backup`, `support`, `intervention`, `umh`, `uhm`, `pro`, `unpro`, `groupspro` | Backup covered locally; provider support, plan and subscription operations are not applicable. |

## Optional additions for your decision

| Addition | Suggested priority | Decision needed |
| --- | --- | --- |
| Private screenshot/file evidence attached to reports | High | Define supported formats, storage retention, who may view originals, and public redaction. This would add input beyond the current two-question report. |
| Stronger raid controls | High | Set join-rate thresholds and recovery behavior; group-wide lockdown can affect ordinary users. |
| Timed warnings, bulk actions and combined delete/punish commands | High | Choose durations, batch size and per-target outcome reporting. |
| Staff group and separate audit channel | Medium | Select private destination IDs and which events to send. Contributor identities should remain restricted. |
| Local blocklist and explicit exemptions | Medium | Choose scope, evidence requirements and appeal/removal process. No proprietary Group Help list is imported. |
| More content controls | Medium | Select media subtypes, forwarded-channel rules, permitted domains, a second phrase list and name checks. |
| Image/NSFW screening | Medium | Select a local model or external provider; assess accuracy, privacy, cost and review workflow. No image classifier is currently installed. |
| Role-specific deletion and per-feature permissions | Medium | Define moderator, cleaner and helper permissions and exemptions. |
| Mandatory channel membership | Medium | Specify required chat IDs, bot administrator rights and how inaccessible membership checks behave. |
| Rich channel editor and topic routing | Medium | Choose button layouts, media handling, editing and forum topic destinations. |
| Activity charts and observed inactive-member tools | Low | Choose retention and reporting windows. Past history and unseen users cannot be reconstructed. |
| Multiple languages, games and giveaways | Low | Choose languages and draw rules. These do not improve the core reporting workflow. |
| Web administration dashboard | Optional | The first release uses Telegram administration and local CLI tools; a separate web login and access model would be needed. |

Owner-only Telegram actions such as transferring ownership, some native chat settings and recovering deleted content remain Telegram app operations. A bot’s permissions and data access cannot exceed the Bot API.
