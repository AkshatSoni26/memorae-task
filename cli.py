import sys
import time
from app.core.config import settings
from app.core.logger import logger
from app.schemas.bot import JoinMeetingRequest
from app.services.meeting_baas import meeting_baas_service

def run_cli():
    print("\n" + "=" * 55)
    print("      🎙️  MEMORAE - GOOGLE MEET AI AGENT CLI")
    print("=" * 55)
    print(f"Agent Persona: {settings.default_bot_name}")
    print(f"LLM Model:     {settings.llm_model}")
    print("=" * 55 + "\n")

    # 1. Get Google Meet URL
    if len(sys.argv) > 1:
        meeting_url = sys.argv[1].strip()
    else:
        meeting_url = input("Enter Google Meet URL (e.g. https://meet.google.com/xxx-yyyy-zzz): ").strip()

    if not meeting_url:
        print("❌ No meeting URL provided. Exiting.")
        return

    # 2. Confirm Bot Name & Pipeline
    bot_name = input(f"Agent Name [{settings.default_bot_name}]: ").strip() or settings.default_bot_name
    pipeline_engine = input("Pipeline Engine (groq/speaking_bots) [groq]: ").strip() or "groq"

    # 3. Dispatch Bot
    print(f"\n🚀 Sending '{bot_name}' to meeting: {meeting_url} using pipeline '{pipeline_engine}' ...")
    try:
        req = JoinMeetingRequest(
            meeting_url=meeting_url,
            bot_name=bot_name,
            pipeline_provider=pipeline_engine
        )
        res = meeting_baas_service.dispatch_bot(req)
        bot_id = res.bot_id
        print(f"\n✅ Bot Dispatched!")
        print(f"   Bot ID:   {bot_id}")
        print(f"   Status:   {res.status}")
        print(f"   Endpoint: {res.provider_endpoint}")
        print("\n👉 Please ADMIT the bot into your Google Meet call now!")
    except Exception as e:
        print(f"❌ Failed to dispatch bot: {e}")
        return

    # 4. Interactive Loop for Live Status or Exit
    print("\n" + "-" * 55)
    print("Options:")
    print("  [s] Check current bot status")
    print("  [q] Make bot LEAVE meeting and quit")
    print("-" * 55)

    while True:
        try:
            cmd = input("\nAction (s: status, q: leave & quit) > ").strip().lower()
            if cmd == "s":
                status_res = meeting_baas_service.get_status(bot_id)
                current_st = status_res.status or "unknown"
                print(f"📊 Current Status: {current_st}")
                if status_res.data and status_res.data.participants:
                    print(f"   Participants: {len(status_res.data.participants)}")
            elif cmd == "q":
                print(f"\n🛑 Instructing bot {bot_id} to leave meeting...")
                leave_res = meeting_baas_service.leave_meeting(bot_id)
                print(f"✅ {leave_res.message}")
                print("Exiting CLI. Goodbye!")
                break
            else:
                print("Invalid command. Type 's' to check status, or 'q' to leave and quit.")
        except KeyboardInterrupt:
            print("\n\n🛑 Force quitting. Attempting to disconnect bot...")
            try:
                meeting_baas_service.leave_meeting(bot_id)
                print("Bot disconnected.")
            except Exception:
                pass
            break
        except Exception as err:
            print(f"Error: {err}")

if __name__ == "__main__":
    run_cli()
