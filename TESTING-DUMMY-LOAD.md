# Testing the booking agent with a dummy load

You play two people: the **warehouse** that books the pickup, and the **account manager** who
watches the board. The app plays Circle's booking agent, and it works on its own: it sends the
request, reads every reply, answers it, and books the pickup when the warehouse confirms. The
account manager only steps in when the board asks. Real emails go back and forth, but only
between two test addresses, about one dummy load. Nothing reaches a real vendor, the Lidl
group, or a real load.

> **Status: plan.** The setup below is not done yet. Steps marked **[setup]** are done once,
> after you say go. The test steps after that are yours.

---

## How it works

```
  Dummy load in Transport Pro
          |   the scan reads only this load
          v
  Booking agent (its own test store)
          |   request email, from booking-test@circle-analytics.com
          v
  warehouse-test@circle-analytics.com  <-- you, as the warehouse, on the "Test warehouse" page
          |   your reply
          v
  booking-test@circle-analytics.com    --> the agent reads it within 2 minutes, answers it,
                                           and books the pickup when you confirmed
                                       --> you, as the account manager, step in only when the
                                           board shows "Needs your attention"
```

| Who | Played by | Where |
|---|---|---|
| Circle's booking agent | The app | Sends from `booking-test@circle-analytics.com` |
| The warehouse's booking desk | You | The **Test warehouse** page, as `warehouse-test@circle-analytics.com` |
| The account manager | You | The board, `http://localhost:8000`, only for what it raises |

**Why both addresses are on circle-analytics.com.** That domain already sends and receives
mail through Amazon SES, and every address on it is a rule that saves incoming mail to S3.
There is no inbox to log in to, so the Test warehouse page is your inbox: it lists what the
agent sent the warehouse and lets you reply. Your real Gmail, the lidl@ group and the Gmail
service-account key are never used. The booking logic is exactly the production code; only how
the mail travels differs (SES instead of Gmail).

## What keeps it safe

- **One load.** The scan reads only the dummy load's number, never a search of the pod.
- **Its own store.** The test uses `data/dummy-test.db`; the real Lidl store is never opened.
- **Two addresses.** The agent only sends to the booking desk on the vendor's profile, and in
  the test that desk is `warehouse-test@`. The test customer file has no cc and no real group.
- **Transport Pro is read, not written.** Writing the booked time back to the dummy load is a
  separate, optional step at the end, and only if you want it.

## Where the emails are kept

Every email is kept in two places:

| Where | What | Why |
|---|---|---|
| **S3** (SES saves each email it receives) | The full original email, as received: one file per email, in a folder for each test address. | A raw copy nobody edits. The Test warehouse page reads the warehouse's folder; the agent reads its own. |
| **The test store** (`data/dummy-test.db`) | Every email on the pickup it belongs to: who sent it, to whom, when, subject, text, the email's ID and which email it answers, and how the agent read it. | What the board shows under **Emails** and **History**, and what the agent decides from. |

The agent's own emails are kept the same way, with the email ID it gave them, so a reply can
be tied back to the exact request it answers.

In production it's the same, except the mail lives in Gmail (the sending mailbox and the
customer group) instead of S3.

## How the agent knows a new email came in, and what it answers

Email isn't pushed to the agent; it **checks**. While the board runs with the test settings,
it checks the agent's address for new email every 2 minutes. **Check for replies** on the board
checks right away. Each new email then goes through these steps:

1. **Seen before?** An email already on file, by its email ID, is skipped, so checking twice
   never answers twice.
2. **Which pickup is it about?** In this order:
   - the email it says it replies to (the email ID of our request);
   - the email conversation (thread);
   - a PO number written in it;
   - the only open pickup with that warehouse.

   If none fits, it's left alone, and it never guesses.
3. **Is the pickup still open?** If it's already booked or canceled, the email is kept on the
   pickup but not acted on.
4. **What does it say?** The agent reads the reply, without the quoted earlier emails under
   it, as one of: confirmed, another time offered, a question, can't ship, check back later, or
   not about this pickup. Every date, time and pickup number it takes must appear word for word
   in the reply; anything else is dropped.
5. **What happens next:**

   | The warehouse said | The agent does, on its own |
   |---|---|
   | Confirmed the time asked for | **Books it** and sends "Thank you!". A time picked by clicking a link is booked the same way. |
   | Confirmed a different time, or anything in doubt | Leaves **Approve confirmation** for you, with the reason ("not booked automatically: ..."). |
   | Another time | Accepts it if the truck still makes the delivery, sends "Yes, ... works", and books it. Otherwise asks for other days. |
   | A question | Answers from what's on the pickup (PO, delivery number, delivery site, load number, carrier, customer). If it can't, raises **Vendor question** for you. |
   | Can't ship | Raises **Vendor cannot book** and sends a note to the customer's desk (a test address in the test). |
   | Check back later | Waits, then asks again on that day; no to-do. |
   | Money, a claim, or the 3rd back-and-forth | Stops and hands it to you. |

   **What it books on.** Only a confirmation of the time asked for: the same day, within two
   hours, not already past. Every date, time and number must be in the reply's own words, with
   nothing about money. The reply must answer the agent's email or name the PO (coming from the
   right sender isn't enough), and the time must still make the delivery.

   **What it sends.** Only to the warehouse's desk on the profile (or the person there who
   wrote), or to the customer's desk for a note. Never more than the daily cap. Anything else is
   kept as a draft and raised for you.

At most one email goes back for each reply. The agent's answer is kept on the pickup like any
other email, so the next reply in the conversation is tied to it the same way.

Changes on the load itself (pickup number, canceled, new delivery slot) don't come by email.
They come from Transport Pro on the next `booking scan`.

---

## One-time setup

| # | What | Who |
|---|---|---|
| 1 | Tell Claude the dummy load's number, and which customer it is billed to | You |
| 2 | Create the two addresses: SES receiving rules that save mail to S3, next to the other circle-analytics.com addresses **[setup]** | Claude, after your OK |
| 3 | Add what the test needs to the app **[setup]**: `booking scan --load` (one load only), `booking test-reset`, `serve --env` (start with the test settings), sending and reading through SES (the agent's inbox is the test address), the Test warehouse page, and a **Check for replies** button on the board. Reading and answering replies on its own is already built (`FP_BOOKING_INBOX`, `serve --autopilot-every`). | Claude |
| 4 | A test settings file, `.env.dummy-test` (the test store, the two addresses, sending on for the test only) **[setup]** | Claude |
| 5 | A test customer file, kept outside the public repository, so the dummy load's emails go only to the test addresses. Its rule turns everything on: `do = "send"`, `replies = "send"`, `confirm = "auto"`, `customer_notes = "send"` **[setup]** | Claude |
| 6 | Make `warehouse-test@` the booking desk of the dummy load's pickup **[setup]** | Claude |

## Start a test session

1. Sign in to AWS (it opens your browser once a day):
   ```
   aws sso login --profile paybot-admin
   ```
2. Start the board with the test settings. The agent then works on its own every 2 minutes:
   it reads new replies, answers them, books, and writes requests that are due.
   ```
   .\.venv\Scripts\python.exe -m facility_profiles.cli serve --no-sign-in --env .env.dummy-test --autopilot-every 2
   ```
3. Open two browser tabs:
   - **Board:** `http://localhost:8000` (you, as the account manager)
   - **Test warehouse:** `http://localhost:8000/test/warehouse` (you, as the warehouse)
4. Pick up the dummy load. The agent sends the request on its next pass (within 2 minutes):
   ```
   .\.venv\Scripts\python.exe -m facility_profiles.cli booking scan --load <dummy load number>
   ```
   **You should see:**
   - The board shows the dummy pickup as **Asked vendor**.
   - Within a minute, the Test warehouse page shows the request, for example: "Can I please
     schedule the following? PO# 1158… on 10/08 @ 0900".
5. After each reply you send as the warehouse, the agent reads and answers it within 2 minutes.
   Its answer appears on the Test warehouse page, and the board shows the result. To skip the
   wait, click **Check for replies**.

---

## The tests

Each test starts from a fresh request (see **Start over** below). "You do" is you as the
warehouse, on the Test warehouse page. "You should see" is on the board and the Test warehouse
page. In tests 1 to 6 you, as the account manager, should **not** have to click anything,
except where a test says the board asks you.

### 1. The warehouse confirms the time asked for
| You do | Reply with the requested date and time: `SET! PU# 55501 10/08 @ 0900` |
|---|---|
| You should see | Within 2 minutes the pickup turns green, **Booked**, with pickup number 55501, and "Thank you!" arrives on the Test warehouse page. The History shows "Booked by the agent". |
| Pass if | Nobody approved it, and the time and number match what you wrote. |

### 1b. The warehouse confirms a different time
| You do | Reply with another time the same day: `SET! PU# 55502 10/08 @ 1400` |
|---|---|
| You should see | **Needs your attention: Approve confirmation**, with "not booked automatically" and the reason (a time more than two hours from the one asked for). |
| Then | As the account manager, click **Approve the vendor's time** if it's fine. |

### 2. The warehouse confirms with one click
| You do | Open the request and click one of the time buttons, for example 10:00. |
|---|---|
| You should see | **Booked** straight away, at the time you clicked. The History shows "Vendor picked a time from the link". |
| Pass if | No approval was needed and the time matches your click. |

### 3. The warehouse offers another time
| You do | Reply: `We can't do 9. How about 1 PM the same day?` |
|---|---|
| You should see | One of two things. If 1 PM still gets the truck to the delivery in time, "Yes, 10/08 @ 1300 works. Thank you!" arrives and the pickup is **Booked**. If it doesn't, the agent writes back asking for other days. |
| Pass if | The agent's email makes sense for the delivery time, and nobody had to approve. |

### 4. The warehouse asks a question
| You do | Reply: `What is the delivery number for this one?` |
|---|---|
| You should see | "The delivery number is ... Thank you!" arrives on the Test warehouse page. A question it can't answer raises **Vendor question** for you instead. |
| Pass if | The answer quotes the right number from the load, never a made-up one. |

### 5. The warehouse cannot ship
| You do | Reply: `This PO is not ready, we cannot ship it this week.` |
|---|---|
| You should see | **Vendor declined**, with the to-do **Vendor cannot book** for you. The agent sends a note to the customer's desk (a test address in the test) asking for a new delivery appointment. |
| Pass if | Nothing is booked, and the to-do says what you wrote. |

### 6. The warehouse asks you to check back
| You do | Reply: `Please check back Monday.` |
|---|---|
| You should see | The pickup stays **Asked vendor**; the agent waits and doesn't treat it as a no. |
| Pass if | No to-do is raised just for that. |

### 6b. The warehouse mentions money
| You do | Reply: `We can do 10/08 @ 0900 but there is a $125 fee if the driver is late.` |
|---|---|
| You should see | Nothing goes back to the warehouse. **Needs your attention** shows it for you ("reply mentions money or a claim"). |

### 7. The warehouse says nothing (optional, takes a day)
| You do | Don't reply. |
|---|---|
| You should see | After 24 weekday hours, the to-do **No reply in 24 h** and one polite reminder to the warehouse. After 48 hours, **No reply in 48 h**. |

### 8. Changes made in Transport Pro (only if you're allowed to edit the dummy load)
| You do | In Transport Pro, on the dummy load: (a) enter a vendor pickup number, or (b) cancel the load. Then run `booking scan --load <number>` again. |
|---|---|
| You should see | (a) **Booked**: "booked in Transport Pro: vendor pickup number …". (b) **Load canceled**, with "tell the vendor the pickup is no longer needed". If nothing had been sent yet, the pickup is simply closed. |

### 9. Writing the booking back to Transport Pro (optional, writes to the dummy load)
Only if you want the booked time written onto the dummy load:
```
.\.venv\Scripts\python.exe -m facility_profiles.cli booking writeback --dry-run
```
This shows what would be written. If it's right, ask Claude to run it for real, once, for the
dummy load only.

---

## Start over

To run the next test on the same dummy load:
```
.\.venv\Scripts\python.exe -m facility_profiles.cli booking test-reset --load <dummy load number>
```
This removes the dummy pickup from the **test store** only; it refuses any other store. Then
repeat step 4 of **Start a test session**.

## Clean up when you're done

- Stop the board with Ctrl+C.
- Optional: delete `data/dummy-test.db`.
- The two test addresses can stay for the next round, or Claude can remove their two SES rules.

## If something looks wrong

| What you see | What to check |
|---|---|
| No request on the Test warehouse page | Did `booking run` say "sent"? Is your AWS sign-in still valid (`aws sso login …` again)? |
| Your reply doesn't show on the board | Wait 2 minutes, or click **Check for replies**. Was the board started with `--autopilot-every 2`? The reply must be sent from the Test warehouse page, as a reply to the agent's email. |
| The agent answered, but as a draft | The send gate refused it (the History says why: not the desk on the profile, or the daily cap). The board raises it for you. |
| The board asks you to sign in | Start it with `--no-sign-in` (Google sign-in waits for its redirect address). |
| The pickup says "No booking desk" | Setup step 6 wasn't done for this load's pickup. |
| Anything else | Open the pickup, then **History**; every step the agent took is listed there. |
