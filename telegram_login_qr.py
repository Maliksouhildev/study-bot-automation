import asyncio
import os
import qrcode
from telethon import TelegramClient

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION_NAME = "telegram_user"

async def main():
    print("=" * 60)
    print("TELEGRAM QR CODE LOGIN")
    print("=" * 60)
    print("1. Connecting to Telegram...")
    
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.connect()
    
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n[OK] Already logged in as: {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    print("2. Generating QR Code...")
    qr_login = await client.qr_login()
    
    # Save QR as image
    img = qrcode.make(qr_login.url)
    img_path = "telegram_qr.png"
    img.save(img_path)
    
    # Try to open the QR image automatically on Windows
    try:
        os.startfile(img_path)
    except Exception:
        pass

    # Print ASCII QR code in terminal
    qr = qrcode.QRCode()
    qr.add_data(qr_login.url)
    qr.print_ascii(invert=True)
    
    print("\n" + "=" * 60)
    print("HOW TO SCAN:")
    print("1. Open Telegram on your phone.")
    print("2. Go to: Settings -> Devices -> 'Link Desktop Device'.")
    print("3. Point your phone camera at the QR code above or in the opened image.")
    print("=" * 60)
    print("Waiting for scan...\n")
    
    try:
        user = await qr_login.wait(timeout=120)
        print(f"\n🎉 SUCCESS! Logged in as: {user.first_name} (@{user.username})")
        print("Session saved permanently to 'telegram_user.session'!")
        if os.path.exists(img_path):
            try: os.remove(img_path)
            except Exception: pass
    except asyncio.TimeoutError:
        print("\n[!] QR code expired. Run the script again to generate a new QR code.")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
