const { launch, getStream } = require("puppeteer-stream");
const fs = require("fs");
const path = require("path");
const axios = require("axios");

// Read arguments
const args = process.argv.slice(2);
const meetingUrl = args[0] || "https://meet.google.com/xxx-xxxx-xxx";
const studentName = args[1] || "Student Name";
const webhookUrl = args[2] || "http://localhost:5678/webhook/meet-done"; 
const minWaitSec = 10;
const maxWaitSec = 60;

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function startBot() {
    console.log(`Starting bot for meeting: ${meetingUrl}`);
    
    // Random delay to seem human
    const delaySec = Math.floor(Math.random() * (maxWaitSec - minWaitSec + 1)) + minWaitSec;
    console.log(`Waiting ${delaySec} seconds before joining...`);
    await sleep(delaySec * 1000);

    const browser = await launch({
        headless: false, // Must be false for puppeteer-stream extension to work in some versions, and for Google Meet to render video
        executablePath: require("puppeteer").executablePath(), // Make sure it uses puppeteer's downloaded chromium
        args: [
            "--use-fake-ui-for-media-stream", // auto-allow camera/mic permissions
            "--disable-infobars",
            "--window-size=1280,720"
        ],
        defaultViewport: {
            width: 1280,
            height: 720,
        },
    });

    const page = await browser.newPage();
    console.log("Navigating to meeting...");
    await page.goto(meetingUrl, { waitUntil: 'networkidle2' });

    console.log("Bypassing pre-join screens...");
    
    // Wait for the name input field (guest join)
    try {
        await page.waitForSelector('input[type="text"]', { timeout: 15000 });
        console.log("Entering name...");
        await page.type('input[type="text"]', studentName);
    } catch (e) {
        console.log("No name input found, might be logged in or requires login.");
    }

    // Turn off camera and mic before joining (optional but polite)
    try {
        // The buttons often have aria-labels like "Turn off microphone"
        const micBtn = await page.$('div[aria-label*="microphone"]');
        if (micBtn) await micBtn.click();
        
        const camBtn = await page.$('div[aria-label*="camera"]');
        if (camBtn) await camBtn.click();
    } catch (e) {
        console.log("Could not toggle mic/cam.");
    }

    // Click "Ask to join" or "Join now"
    try {
        const joinButton = await page.$x('//span[contains(text(), "Ask to join") or contains(text(), "Join now")]');
        if (joinButton.length > 0) {
            await joinButton[0].click();
            console.log("Clicked join button. Waiting to be admitted...");
        }
    } catch (e) {
        console.log("Could not click join button.");
    }

    // Wait until we are in the meeting
    // A good indicator is the "Leave call" button becoming available
    try {
        await page.waitForSelector('button[aria-label="Leave call"]', { timeout: 120000 }); // Wait up to 2 mins to be admitted
        console.log("Successfully joined the meeting!");
    } catch (e) {
        console.log("Failed to join or timed out waiting to be admitted.");
        await browser.close();
        return;
    }

    // Start recording
    console.log("Starting recording...");
    const stream = await getStream(page, { audio: true, video: true, frameSize: 1000 });
    const startTime = Date.now();
    const fileName = `meeting_${startTime}.webm`;
    const filePath = path.join(__dirname, fileName);
    const writeStream = fs.createWriteStream(filePath);
    
    stream.pipe(writeStream);

    // Monitoring meeting state to auto-leave
    let inMeeting = true;
    while (inMeeting) {
        await sleep(10000); // Check every 10 seconds
        
        // Check if meeting has ended or everyone left
        try {
            // Check participant count, if less than 2, maybe leave?
            const participantCountElement = await page.$('div.uGOf1d');
            if (participantCountElement) {
                const text = await page.evaluate(el => el.textContent, participantCountElement);
                const count = parseInt(text);
                if (!isNaN(count) && count < 2) {
                    console.log("Only 1 person left (just the bot). Leaving meeting...");
                    inMeeting = false;
                }
            }
            
            // Also check if we got kicked
            const returnToHomeScreen = await page.$x('//span[contains(text(), "Return to home screen")]');
            if (returnToHomeScreen.length > 0) {
                console.log("Meeting ended or got kicked.");
                inMeeting = false;
            }
        } catch (e) {
            // Error checking, assume still in meeting
        }
    }

    // Stop recording
    console.log("Stopping recording...");
    stream.destroy();
    await sleep(2000); // give it time to flush

    const durationSec = Math.floor((Date.now() - startTime) / 1000);
    console.log(`Meeting duration: ${durationSec} seconds. File saved to ${filePath}`);

    await browser.close();

    // Trigger n8n webhook
    if (webhookUrl && webhookUrl.startsWith("http")) {
        console.log(`Sending webhook to n8n at ${webhookUrl}...`);
        try {
            await axios.post(webhookUrl, {
                meetingUrl: meetingUrl,
                duration: durationSec,
                videoPath: filePath,
                studentName: studentName,
                recordedAt: new Date().toISOString()
            });
            console.log("Webhook sent successfully!");
        } catch (e) {
            console.error("Failed to send webhook:", e.message);
        }
    }
}

startBot().catch(console.error);
