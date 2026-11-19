# Seed to Profit: the manual

## The job

Your team starts a consumer goods company with $2,000,000 of seed money. The company sells physical products online,
on its own site and on a marketplace. You choose what to sell from a catalog of 30 products, and you write the
software that runs the company: prices, ads, stock orders and money, every day for three years.

You have 4 hours. You work as a team: every agent has its own machine and a clone of the team repository, and
you talk on the board.

The first hour is the launch phase. The company does not exist yet: you research the products and write the plan
and the software. Then the company opens, and its three years run while you work: one simulated month every 4.7
minutes of real time, each month with the software on `main` at the moment the month starts, until the last month
ends a few minutes before your time is up. You watch the company through its books (the office's free tools,
section "The market office") and change the software whenever you like; a new commit takes over at the next
month. The company is the one you run: there is no second chance at the end.

Your investors are also betting on what you learn: the software and plan you leave behind are a playbook they can
use to start and grow more companies like yours. After the run, your final `main` starts 24 new companies in new
markets, each running for three years on its own, with no one at work on it. Half the score is what your company
is worth; half is how the playbook's companies do (section "How you are scored").

You never see the market's code. The market office sells research about the products and the market, and runs
your software in practice markets (test markets) where you can try a change before it reaches your company.

## What you deliver

These files are on `main`.

- `company/plan.yaml`: the company when it opens. It is read once, when the launch phase ends. Up to
  10 products from the catalog, the channels each one sells on (`site`, `marketplace` or both), the
  supplier each one is ordered from by default (`A`, `B` or `C`, default `A`), and the credit line to ask for (0 to
  $500,000; how much of it you can draw follows your stock and receivables, section "Rules"). A plan that is
  missing or invalid then opens the company with no products; the software can still launch products later.

  ```yaml
  company: Example Goods
  credit_line: 250000
  products:
    - id: k1
      channels: [site, marketplace]
      supplier: A
  ```

- `ops/autopilot.py` and anything else in `ops/` it needs: the software that runs the company (section "The
  software"). Each month runs `ops/` as it is on `main` when the month starts. The company's software runs with
  the same Python and packages as your machines.

`company/`, `ops/` and `program/` may hold only plain files and folders. A symbolic link anywhere in them stops the
company's software (every month runs with nothing changed) and makes the score 0.

## How you are scored

The score has two halves:

```
score = 0.5 x live score + 0.5 x playbook score
```

The live score is what your company is worth when its three years end. The playbook score is the mean score of the
24 companies your final software and plan start after the run (below). Both are scored the same way. A company's
value is:

- cash, including what the marketplace and retail chains still owe you,
- plus stock on hand and stock that has shipped, at 35% of its landed cost (what a liquidator pays),
- minus debt (the credit line and any venture debt) and the tax owed, and the unsold units chains still running a
  contract would send back,
- minus the refunds and return shipping still to come for sales already made (less the resellable units),
- plus 3 times the company's operating profit over the last 365 days, if that profit is positive. Those
  refunds still to come also come off that profit, so they count 1 + 3 times in all.

Operating profit is counted when sales happen (accrual): revenue less refunds, the landed cost of the units sold,
fulfillment, fees, ads, storage, overhead, the cost of each line, launch costs and development projects. It leaves out
interest, tax, the fees of raising money and the money paid for stock that is not yet sold.

A bankrupt company is worth 0. Then

```
m     = company value / $2,000,000 - research spent / $2,000,000
score = 0 if bankrupt, else min(1, max(0, log2(m) / 5))
```

Each doubling of the seed money adds 0.2: keeping the seed money (m = 1) or losing money scores 0, twice the seed
0.2, four times 0.4, and 32 times or more 1. Research money counts against the score. If the company sold
new shares (section "Growing the company"), the investors are paid first, on the usual venture terms: each round
takes the larger of the money it put in and its share of the company value, and the team's value is what is left
(the lender's warrants share it with the team).

If the run ends before the company's last month (for example, the host stopped for a while), the grader runs the
days left with the software on `main` at the end. It then replays the company from its record (the commit each
month ran) in a fresh copy of the world; if the replay ends lower than the live company, the lower value counts.
Software that decides only from what it is given (its inputs and its state folder, with seeded random numbers)
replays exactly.

The playbook: after the run, the grader starts a new company in each of 24 fresh markets, with `company/plan.yaml`
and `ops/` as they are on `main` at the end. Each runs its three years from day 0 with your software alone (nothing
you change after the run reaches it), one world after another, and is scored as above, with the research your team spent. The markets are
new worlds of the same kind as yours (section "The market"): other shoppers, rivals, economies, shocks, hidden
changes and countries, the same products and the same announced change. Three quarters have the shapes of the
office's practice markets (`mnl`, `nested`, `wom`), a quarter the other two (`delayed_ads`, `deal_seekers`).
Your software is not told that it runs in one of them. Software tuned only to your own market can do well there
and fail in the others.

## The software

The company calls your software once per simulated day, for 1095 days starting 2027-03-01. `ops/autopilot.py` must
define

- `init(setup)` (optional), called when a process starts, and
- `step(obs)`, called every day, which returns the day's actions as a dict.

Each month of your company starts a fresh process with `ops/` from `main` at that moment: `init(setup)`, then
`step(obs)` for each day of the month. To keep anything across months, write it to `setup["state_dir"]`: that
folder is kept from one month to the next, whatever the commit (plain files only, at most 50 MB). In the live
company `setup["live"]` is `{"month": 7, "day": 210, "commit": "<sha>", "resumed": true}`; it is missing in test
markets, where the state folder starts empty and one process runs the whole market.

Limits per month of your company: 20 seconds for any one call, 60 seconds in all (wall time), 2 GB of memory and
one BLAS thread. A test market gives 100 seconds for its 182 days, and each playbook world 600 seconds for its
1,095 days: the same pace, so a test market shows whether your software fits the playbook. The software runs as a
user of its own, from its own copy of `ops/`, with a private temporary folder and the state folder. It may not read
anything else, and must not use the network: sockets, System V IPC and POSIX message queues are blocked (those
calls fail) and the shared temporary folders cannot be written, while pipes and subprocesses work. The state
folder must stay a plain folder of plain files: a link in it, or in its place, means it is not kept. If a call fails, times out or returns something that is not a JSON object, the
software is stopped until the next month (in a test market or a playbook world, for the rest of it), and its last
prices and ad budgets stay in place. The month's summary in the books says so. Print
to stderr or stdout freely; it does not reach anyone.

`setup` holds `days`, `start_date`, `rules` (the table in section "Rules"), `sizes`, `season`, `shapes` (the names
of the five model shapes), `products` (the catalog entries of the company's products, with `channels`,
`landed`, `landed_break` and `fees`), `catalog` (all 30 products, for launching later), `credit_line` (the line in
the plan), `credit_limit` (what you can draw today), `cash`, `countries` (the table in section "Countries", by
country code), `state_dir` and, in the live company, `live`.

`obs` holds

- `day`, `date`, `days_left`, `cash`, `credit_line`, `credit_limit` (what you can draw today: the line, up to the
  borrowing base), `credit_used`, `marketplace_receivable` (all the marketplace owes you, including sales it still
  holds), `marketplace_fee`;
- `news`: a list of today's news items, usually empty. Each item is a dict: `{"day": 120, "kind": "tariff",
  "starts": 150, "text": "A tariff of 20% on the supplier price of kitchen goods applies to orders placed from day
  150."}`. `kind` is one of `tariff`, `supplier_closure`, `port_delay`, `platform_fee` (each with `starts`, the day
  it takes effect, 30 days later), `holiday_ads` (every October 1, no `starts`), `market_change` (the office's
  announced change, 30 days before it starts), and these about your own company:
  `development` (with `product` and `worked`), `wholesale` (a contract ended or was cancelled), `term_sheet`
  (investors made an offer), `vendor` (a vendor's pitch, with `offer`; see "Vendors"), `suspension` (the
  marketplace suspended a listing, with `product` and `until`), and two about rivals, `rival_closed` and
  `rival_entered` (with `product`), and `country` (the company is entering a country, with `country` and `opens`).
  A country's own news (its tariffs, port delays, fee rises, `currency` drops) is in its section of `countries`.
  The `text` names the category or product;
- `products`, by id: `active`, `channels`, `price` and `ad_budget` now in force, `on_hand`, `inbound` (open supplier
  orders: `po`, `qty`, `status` ordered or shipped, `expected` (the quote), `arrives` (once shipped), `unit_cost`,
  `supplier`), `rating`, `reviews`, `supplier` (the line's default), `unit_cost_now` and `lead_quote` (from that
  supplier), `supplier_quotes` (for `A`, `B` and `C`: `unit_cost`, `lead`, `moq`, with tariffs),
  `rival_prices` and `rival_ratings` (open rival listings; [] when every rival closed), `improved` (an own improved version replaced it), `development` (a running
  project: `started`, `result_day`, or null), `subscription_discount`, `subscribers`, `site_customers` (people
  on your list who bought this product on the own site), `suspended_until` (the
  listing is off the marketplace before this day; 0 if it never was), and `yesterday`: for each
  channel `sessions`, `orders`, `units`, `revenue` and `in_stock` (the site also `addon_units` when other
  lines' orders added this product, and `bundle_orders` when bundles sold);
  for each ad channel `spend`, `impressions`, `clicks` and `attributed_orders` (the ad platform's own count, which
  runs high); `returns_units`, `refunds`; `subscription` (`subscribers`, `orders`, `unfilled`, `revenue`, `left`)
  on a day it is on sale on the own site with subscribe and save running, even with no subscribers yet (a stopped
  line keeps its subscribers but has no `subscription`); and `wholesale` (`contract`, `units`, `short`,
  `invoiced`, `shelf_sales`) on a day a chain's delivery of it was due. Check that a part is there before reading
  it;
- `wholesale_offers`: open offers from retail chains, each `{"id": "W3", "chain": ..., "product": "k1",
  "units_per_month": 2400, "price": 15.8, "months": 12, "pay_days": 60, "expires": 419}`;
  `wholesale_contracts` (running: `id`, `chain`, `product`, `units_per_month`, `price`, `months`, `deliveries`,
  `next_delivery`, `strikes`, `shelf_sales` (what the chain sold from its shelf in the month before the last
  delivery)) and `wholesale_receivable`;
- `funding`: `equity_offer` (an open term sheet `{"pre_money", "max_amount", "fee", "expires"}`, or null) and
  `venture_debt` (`{"max_amount", "apr", "warrant_coverage", "valuation", "warrant_share"}` when a lender would
  lend, or null; `warrant_share` is for `max_amount`); `ownership` (the
  share of the company the team owns, 1 at the start), `equity_rounds` (each round sold: `{"day", "amount",
  "share"}`, its share after later rounds) and `venture_debt_used`;
- `bundles`: the bundles on the own site now;
- `vendor_offers`: open pitches from vendors, each `{"id": "V2", "vendor": ..., "kind": ..., "product": ... or
  null, "expires": 380, "text": ...}`. The text says what the vendor offers and how to reply;
- `countries`: each country the company has entered, by code (section "Countries"): `opens` (the day its listings
  start to sell), `selling`, `left` (the company left it), `fx` (the local currency's value in dollars against the
  start, after inflation), `vat`, `marketplace_fee`, `marketplace_receivable`, `products` (the same as above for
  the lines listed there, each with its `typical_price` there, in dollars with VAT), `news`, `bundles` and
  `vendor_offers`. Everything above that is not under `countries` is the US, or the whole company (cash, credit,
  funding, ownership, retail chains);
- `transfers`: stock moving between warehouses, each `{"id", "product", "qty", "from", "to", "arrives"}`.

This is part of a real observation, with one product:

```python
{
  "day": 240, "date": "2027-10-27", "days_left": 855, "cash": 1406232.18, "credit_line": 250000.0,
  "credit_limit": 31416.5, "credit_used": 0.0, "marketplace_receivable": 18452.6, "marketplace_fee": 0.15, "news": [],
  "products": {"k1": {
    "active": True, "channels": ["marketplace", "site"], "price": {"site": 34.0, "marketplace": 34.0},
    "ad_budget": {"social": 100.0, "sponsored": 60.0}, "on_hand": 812,
    "inbound": [{"po": "PO7", "qty": 1500, "status": "shipped", "expected": 248, "arrives": 251, "unit_cost": 11.6}],
    "rating": 4.31, "reviews": 57, "unit_cost_now": 11.6, "lead_quote": 70,
    "rival_prices": [32.9, 35.5], "rival_ratings": [4.3, 4.1],
    "yesterday": {
      "site": {"sessions": 162, "orders": 4, "units": 5, "revenue": 170.0, "in_stock": True},
      "marketplace": {"sessions": 288, "orders": 31, "units": 38, "revenue": 1292.0, "in_stock": True},
      "ads": {"social": {"spend": 100.0, "impressions": 8130, "clicks": 121, "attributed_orders": 5},
              "sponsored": {"spend": 60.0, "impressions": 15000, "clicks": 52, "attributed_orders": 9}},
      "returns_units": 2, "refunds": 68.0}}}
}
```

On day 0 `yesterday` is empty. After that it always has `site` and `marketplace`: a channel where the product was not
on sale yesterday (not listed there, paused or suspended) is an empty object `{}`. `ads` has only the ad
channels that ran: `social` with the site, `sponsored` with the marketplace.

Actions (every key is optional, and what you do not set stays as it was):

```python
{
  "prices": {"k1": {"site": 36.0, "marketplace": 33.5}},    # per unit, 0.25 to 4 times the typical price
  "ads": {"k1": {"social": 120.0, "sponsored": 60.0}},      # dollars per day, 0 to 50,000
  "orders": [{"product": "k1", "qty": 1000, "supplier": "B"}], # at least that supplier's min order; supplier optional
  "credit": {"draw": 50000, "repay": 0},
  "credit_line": 300000,                                        # ask for a different line, 0 to $500,000
  "stop": ["o2"],                                               # stop selling a line
  "launch": [{"id": "h2", "channels": ["marketplace"]}],      # start a new line, at most 10 selling at once
  "develop": ["k1"],                                            # start developing an improved own version
  "accept_offers": ["W3"],                                      # sign a retail chain's offer
  "subscriptions": {"k4": 0.10},                                # subscribe and save on the own site; 0 ends it
  "bundles": [{"products": ["k1", "k2"], "discount": 0.15}],   # replaces the list of bundles; [] removes them
  "raise": {"equity": 1500000},                                 # or {"venture_debt": 800000}
  "vendors": {"V2": {...}},                                     # reply to a vendor's pitch, as its text says
  "enter": ["DE"],                                              # enter countries (section "Countries")
  "leave": ["DE"],                                              # leave countries
  "countries": {"DE": {"prices": ..., "ads": ..., "orders": ..., "stop": ..., "launch": ...,
                       "subscriptions": ..., "bundles": ..., "vendors": ...}},   # a country's own actions
  "transfer": [{"product": "k1", "qty": 500, "from": "US", "to": "DE"}],   # move stock between warehouses
}
```

The top-level `prices`, `ads`, `orders`, `stop`, `launch`, `subscriptions`, `bundles` and `vendors` are the US
market's. Each country takes the same keys under `countries`, for its own market; there `launch` lists a line in
that country. An order can also name its warehouse at the top level: `{"product": "k1", "qty": 1000, "to": "DE"}`.

Social ads bring visitors to the own site. Sponsored ads place the product higher on the marketplace. A product
on one channel only spends nothing on the other channel's ads. Refused actions (an order below the minimum, an
order or launch you cannot pay for) are skipped and the market goes on.
A stopped line can be launched again, at the launch cost; it keeps what it had: stock, reviews and rating,
subscribers, site customers, rivals, and a marketplace suspension that has not ended.

`tools/check_ops.py` runs your software on made-up observations to check the interface and its pace (with every
country entered, as many as a run can enter), including a restart
with the state folder. The office's `plan_review` tool runs it for 14 days under the real rules.

## The market

These are the models the market uses. Their numbers are hidden, and differ by product, by run or by market as
noted. Research measures the products' facts.

### Products (hidden facts, fixed for the run)

Each product has a market size (people who could want it), a price sensitivity, how often a visitor buys at the
typical price on each channel, how satisfied buyers are (review stars), a defect rate, a return rate, how fast
word of mouth spreads, how often buyers buy again, units per order, how it responds to ads, and for each of its three
suppliers the true defect rate, lead times, fill rate and chance of shipping late.

### Demand

- Every shopper chooses between your product, the rivals' products, and buying nothing. The choice depends on
  price, on the price against what the shopper is used to paying (a price above that hurts more than the same
  price below it helps, by an amount that differs by product, and what shoppers are used to follows your price
  over a few months), on the review rating, and on how much the product appeals.
- Reviews: a better rating sells more. A listing with no reviews sells much less than one with many, since
  shoppers do not trust it yet; most of that gap closes within the first 5 to 10 reviews.
- Past buyers buy again at a rate that differs by product: often for things that get used up or replaced, now
  and then for durable goods. Most of them buy ours again, unless its price has risen well above what they paid
  or rivals got much cheaper.
- Whose customer a buyer is depends on where they bought. Buyers on the own site are yours: they are on the
  brand's email and SMS list ($0.015 per person per 30 days), buy again somewhat more often than the product's
  base rate, and buy again on the own site, with no ad cost and no marketplace fee. Buyers on the marketplace belong
  to the marketplace, which keeps their contact details: they buy again on the marketplace, paying its fees, and
  only a few come to the own site (and join the list when they do). Buyers whose channel a line no longer sells
  on look for it on the other one.
- Orders each day are random around the expected number. Sales are capped by stock. You do not see the sales
  you lose when you run out.
- Season: demand follows the monthly factors in section "Season" for the category. How strong the season is
  differs by market.
- The economy: some markets have a recession, which lowers demand and makes shoppers more sensitive to price.
- Product life cycles: each product's market grows or fades through the three years, at a rate that differs by
  market. Some products are fads that peak and then fall.

### The own site

- Visitors come from social ads, from people who know the brand, and from past buyers who buy again (see
  "Demand").
- Social ads: spend works over several days (adstock), and each further dollar a day brings fewer clicks, because
  the cost per click rises with spend. Ads also teach the brand to more people than click, who may visit later, so
  part of what ads bring arrives over the following months and is not in the platform's count.
- Site orders often hold more than one item: an order of one line adds items of your other site lines that are in
  stock, more when more lines sell on the site, and less often for a line priced above its typical price. They
  ship in the same parcel at a small extra pick cost, and show as `addon_units` in that line's `yesterday.site`
  (counted in its `units` and `revenue`, not its `orders`). A marketplace order holds only your product.
- People learn the brand from ads and from word of mouth from buyers, up to the product's market size, and
  forget it again over about a year.

### The marketplace

- Shoppers search for the product type every day. Those who bought yours mostly do not search again (they buy
  again as past buyers).
- Your listing's visibility depends on your sales of the last 30 days (sales rank), on your rating once you have
  some reviews, and on sponsored ads. Visibility sets how many shoppers look at your listing, and each one then
  chooses between yours, the rivals' and nothing.
- The marketplace takes a referral fee on each sale and a fulfillment fee per unit. It pays out every 14
  days, but only for sales at least 10 days old (delivery, then a 7 day reserve), so money from a sale arrives
  10 to 24 days later. Stock at the marketplace costs more to store from October to December.
- The marketplace's review policy, from its seller terms: sellers may not pay for reviews, or give anything of
  value for one, except through the marketplace's own early reviewer program. When it finds reviews that break the
  policy it removes them and can suspend the listing. While a listing is suspended nothing sells on the
  marketplace, sponsored ads stop, and the marketplace holds back part of your payouts until the listing is back.

### Rivals

Every product has 2 or 3 rivals, who set prices once a week. Some keep their price and run a holiday sale, some
set the best price against your last week's price, some learn their price by trial and error, and some undercut
you when you are cheaper. A product that sells well on the marketplace can draw copycat sellers.

Rivals are businesses too. Each pays fixed costs from its margins, and one that loses money long enough closes its
listing; sellers also stop for reasons of their own. New sellers come in now and then, more often into a market
that lost one.

### Vendors

Vendors pitch services now and then. A pitch arrives as news (kind `vendor`) and stays in `vendor_offers` until it
expires; reply with the `vendors` action, as the pitch says. A pitch says what the service offers and what
it costs.

### Suppliers, stock and returns

- Every product has three suppliers. `A` (standard) is the catalog row. `B` (budget) quotes 20% less, 12 days longer
  and twice the minimum order. `C` (premium) quotes 18% more, 10 days shorter and half the minimum order. How many
  defects each one ships, how late and how short it ships, differs by product and is hidden: a budget supplier may
  be nearly as good as the standard one, or much worse. Units on hand carry the defect rate of the orders they
  came from.
- An order must be at least the supplier's minimum order. Orders of at least 5 minimum orders get
  8% off the supplier price. You pay 30% when you order and the rest when the goods ship, 35 days before they
  arrive.
- Quoted lead times include production and about five weeks at sea and at the port. They are a guide. True lead
  times are random, some shipments are late, and a supplier may ship less than you ordered (you get the deposit
  back on what it did not ship).
- About 4% of units sold get a review about 10 days later. Defective units are always returned and
  get bad reviews.
- Returns come back 5 to 25 days after the sale. You refund the price and pay return shipping, and
  60% of the returned good units can be sold again.

### Ads prices

Ad prices rise in November and December. In some markets an ad platform change raises social ad prices for
good partway through.

### Changes you are not told about

In a market, 1 to 3 things change partway through without notice: a product's market shrinks or grows, a
category becomes more sensitive to price, ads cost more, or a new rival enters a product's market.

### Shocks you are told about

The news announces these 30 days before they start: a tariff on a category's supplier prices, a supplier
closing (the standard supplier `A` only: its open orders move to a backup, late, and new orders from `A` cost more
and take longer), port congestion that delays shipments, and a rise in the marketplace fee.

### The announcement

During the run, about a month of your company before its month 18, the market office announces one lasting
change to the market. It is one of: a higher marketplace fee, a large tariff on two categories from every supplier,
dearer ads, or a large established seller entering two categories. You learn which when it is announced
(`office_info()`, `office_status()`, `company_status()`, the company's news and a wake-up), with the day it starts.
It applies to your company from that day, and to test markets started after the announcement from their first
day.

### Model shapes

The market comes in five shapes. The docs name them, but not which market uses which.

- `mnl`: the choice model is a multinomial logit.
- `nested`: shoppers first choose between buying in the category and not buying, then between products, so
  products substitute more for each other.
- `wom`: stronger word of mouth, and ads that cost more.
- `delayed_ads`: ads work with a delay of about a week, bring fewer clicks at once, and more people who come back
  later.
- `deal_seekers`: stronger reference price effects, and a share of shoppers who only buy on a discount of at least
  10% below what they are used to paying, and wait for one.

Your company's market has one of the five shapes. The office's practice markets (`test_market`, `ad_test`) use
only `mnl`, `nested` and `wom`.

## Growing the company

Besides selling more of the catalog products, there are four ways to grow. Each can make the company worth much
more, and each can lose money. Their facts are hidden and differ by product (and for bundles, by pair). The office
sells research on each.

- **Develop an own improved version** (`develop`). A project costs $90,000 (operating cost, paid at once) and
  takes 4 to 8 months. It may not work; then the money is gone and you may try again. If it works, the improved
  version sells from then on: shoppers may like it more, mind its price less, and rate it higher, but how much
  differs by product and may be little or nothing, and its supplier price is 6% to 35% higher for every supplier.
  Copycats compete less well against an improved product. At most 3 projects at once.
- **Sell to retail chains** (`accept_offers`). Once a line has at least 10 reviews and a rating of 3.8 or more,
  chains may offer to stock it for 6 or 12 months at a wholesale price of about 35% to 55% of the typical price.
  The price is net of the chain's trade spend (promotions and allowances it would otherwise deduct). Every 30 days
  from signing you deliver from stock on hand what refills the chain's shelf to `units_per_month`: the first
  delivery fills it, and later ones replace what the chain sold, so how much a chain buys follows how well the
  product sells off its shelves. You pay pallet freight. Chains pay 30 to 90 days after each delivery
  (the offer's `pay_days`), less
  deductions for penalties and shortages (chargebacks). A delivery more than 10% short costs 10% of the
  missing units' value, and two short deliveries end the contract. A chain may end a contract early, most often
  when the product sells slowly. When a contract ends, the chain sends back part of what is left on its shelves,
  for a refund. While a chain sells a product, some of your own site and marketplace buyers buy it there instead,
  others notice the brand on the shelf, and your own sales may end up lower or higher. Offers expire after 14 days.
  At most 6 contracts at once.
- **Subscribe and save** (`subscriptions`) on the own site, at a discount from 5% to 30% off the site
  price. Some of the new site buyers subscribe and then reorder on a schedule, from stock. Subscribers leave at
  their renewals: many at the first two, fewer once they have stayed a while. More leave when you raise the site
  price, and a third of those whose order could not be filled leave. Worth most for products people buy again.
- **Bundles** (`bundles`, up to 5) of two lines on the own site at a discount from 5% to
  30%. Some site buyers of one product add the other. Some of those would have bought both anyway and now pay
  less. Pairs from the same category go together more often.
- **Raise money** (`raise`). From day 120, once revenue is running at $1,000,000 a year (last 90 days), investors
  may offer to buy new shares (`funding.equity_offer`): you may take up to `max_amount` (at most 30% of the
  pre-money valuation, and $4,000,000) at the offered `pre_money` valuation, pay 4% in fees, and the team's
  share of the company falls to pre_money / (pre_money + amount). Investors pay for the company's net cash at face
  value. They price the rest of the company at the larger of the grading formula today (with the last 90 days'
  operating profit as the yearly pace) and a multiple of the yearly revenue run rate that rises with revenue
  growth over the last year (for a company younger than that, its growth over the last two quarters). So a company that grows fast can raise before it makes money, and one that shrinks
  gets a low price and rarely an offer. The price also moves with investors' mood, which differs by run and by
  market. In a recession investors pay less and make fewer offers. At most one term sheet every 180 days,
  open for 30 days. A lender offers venture debt once revenue runs at $1,500,000 a year: up to 30% of
  yearly revenue, at most $2,000,000, at 13% a year. The lender also takes warrants: a share of the company
  equal to its warrant coverage (3% to 10% of the loan, by market) times the loan, over its
  valuation of the company (the last round's post-money valuation, or else its own estimate), at most 25%. The
  debt is repaid at the end (it counts as debt in the company value).

The score counts only the team's part of the company. Each round's shares carry a 1x non-participating
liquidation preference: at the end the round takes back the money it put in, or its share of the company value if
that is more, and the team gets the rest. So money raised and kept as cash goes back to the investors (less the
fees, which the team pays): selling shares pays when the money grows the company by more than it costs, and a high
price helps by leaving the team a larger share when the company ends up worth much more than the investors put in.

## Countries

The company sells in the US from the start. It can enter any of the countries below during the run, and leave them
again. Each country is a market of its own: its own shoppers, rivals, marketplace (fees, payouts, review policing),
own-site traffic and ad prices, its season strength, economy, shocks and hidden changes, and its currency. Cash,
the credit line, debt, investors and ownership stay with the one company, so every country draws on the same money
and a loss in one country can sink the company. Product lines are the company's: at most 10 lines sell
at once, counting a line once however many countries list it.

- **Entering** (`"enter": ["DE"]`) costs the country's entry cost at once (tax registration, compliance, the
  marketplace account, translation of the site) and its listing cost for each line selling in the US, which are
  listed there with the same channels. The listings sell from `days to open` later. Stock can be ordered into the
  country's warehouse from the day you enter. While the company is in a country it pays the country's running costs
  every 30 days (tax agent, compliance, customer service in the language). Operating profit counts the entry,
  listing and running costs. Other lines can be listed later (`countries.DE.launch`): the listing cost, plus the
  launch cost ($6,000) if the product never launched anywhere before. Overhead and the cost of each line are the
  company's, wherever its lines sell. Where a local company must hold paid-up capital (the table's `capital`),
  entering needs that much free cash and credit on top of the costs; the capital stays the company's money.
- **Leaving** (`"leave": ["DE"]`) sells off the country's stock, on hand and at sea to it, at 35% of
  its landed cost, cancels open supplier orders to it (their deposits are lost) and stops its costs. The
  marketplace still pays out what it owes, and returns still come back. Entering again costs the entry cost again.
- **Developments and retail chains go through the US line.** `develop` needs the product's US line selling (the
  improved version then sells everywhere it is listed), and retail chains buy only in the US, so they make offers
  only for US lines.
- **Stock** is bought from the same suppliers into one warehouse per country: an order goes to the US unless it
  names another (`"to": "DE"`, or under `countries`). The catalog's supplier prices include the US duty (about
  15.8%); landed cost in a country is the supplier price without it, times 1 + that country's duty, plus the
  catalog freight times the country's freight factor; lead times change by the country's lead days. A country's
  tariffs (news kind `tariff` in its section) apply to orders into it. `unit_cost_now`, `lead_quote` and
  `supplier_quotes` in a country's products are its own. **Transfers** move stock between warehouses: by road within
  the EU, between the UK and the EU, and between the US, Canada and Mexico, 0.5 times the catalog freight
  per unit and 6 days; else by sea, 1.5 times the catalog freight and 30 days. Except
  within the EU (one customs area, whose members share their tariffs), the destination's duty and tariffs are paid
  again on the supplier price. Freight and duty are paid when the stock leaves and added to its cost.
- **Prices** are in dollars and include VAT where there is VAT: revenue is the price without VAT. The typical
  price (`typical_price`) is the US one times the country's typical price factor, and rivals start near it. The
  marketplace fee is on the price with VAT. Fulfillment, storage and return shipping are the US fees times the
  country's fulfillment factor; returns are the product's return rate times the country's returns factor; social
  and sponsored ads cost the US prices times the country's factors. These costs are in the local currency.
- **Shoppers**: each product's market there is about the US one times the country's market size (its online
  sales against the US, over the square root of its price level, and the share foreign sellers can reach), and
  differs by product: what sells well in one country may sell less in another. Shoppers there are as sensitive to
  price as US ones, against the country's typical price. In Australia and Brazil the outdoor season is the other
  way round.
- **Currency**: money is in dollars. `fx` is the local currency's value in dollars against the start (after
  inflation). Shoppers compare your dollar price, in their currency, with rivals' prices and what they are used to
  paying; those follow part of a currency move (some rivals import and pay in dollars too). So when `fx` falls, your
  prices look dearer there and you sell less unless you cut them, while local costs (fulfillment, storage, ads) get
  cheaper in dollars. It moves every day, with the yearly spread in the table, and now and then it drops suddenly
  (news kind `currency`). Countries with the euro share it.
- **Shocks**: a country has its own recession, tariffs, port delays, marketplace fee rises, rivals and hidden
  changes. Some are global and hit every country at once: a recession that starts in the US half the time, the ad
  platform change, product life cycles, a supplier closing, and the office's announcement.
- **Tax**: one company tax on the company's profit, at the US rate.
- **The books**: `company_books` has a section per country entered. `market_report` takes a `country` (entered or
  not). A `test_market` may enter only the countries your company has entered.

| column | what it means |
|---|---|
| market size | each product's market there, against the US (before the product's own factor) |
| typical price | the typical price there, with VAT, against the US one |
| freight | the catalog freight per unit is multiplied by this |
| lead days | added to the US lead time |
| fulfillment, returns, social ads, sponsored ads | the US figure is multiplied by this |

| code | country | e-commerce (USD bn) | market size | typical price | VAT | duty | freight | lead days | marketplace fee | fulfillment | payout every / held (days) | returns | social ads | sponsored ads | currency, yearly spread | entry cost | days to open | per 30 days | listing per line | capital |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| GB | United Kingdom | 177.0 | 0.170 | 1.15 | 20.0% | 2.9% | 0.69 | +18 | 15.0% | 1.00 | 14 / 10 | 0.90 | 0.81 | 0.48 | GBP, 6% | $8,000 | 42 | $2,500 | $300 | - |
| DE | Germany | 124.2 | 0.127 | 1.07 | 19.0% | 4.1% | 0.69 | +19 | 15.0% | 1.10 | 14 / 10 | 1.30 | 0.84 | 0.48 | EUR, 8% | $15,000 | 77 | $4,000 | $1,000 | - |
| FR | France | 79.2 | 0.080 | 1.09 | 20.0% | 4.1% | 0.69 | +19 | 15.0% | 1.50 | 14 / 10 | 0.90 | 0.67 | 0.45 | EUR, 8% | $18,000 | 77 | $4,500 | $1,000 | - |
| ES | Spain | 56.4 | 0.063 | 1.01 | 21.0% | 4.1% | 0.73 | +19 | 15.0% | 1.20 | 14 / 10 | 0.90 | 0.58 | 0.31 | EUR, 8% | $18,000 | 77 | $4,000 | $1,000 | - |
| IT | Italy | 43.4 | 0.047 | 1.04 | 22.0% | 4.1% | 0.73 | +19 | 15.0% | 1.30 | 14 / 10 | 0.90 | 0.62 | 0.31 | EUR, 8% | $18,000 | 77 | $4,000 | $1,000 | - |
| NL | Netherlands | 23.8 | 0.024 | 1.13 | 21.0% | 4.1% | 0.69 | +17 | 12.0% | 1.15 | 14 / 10 | 1.10 | 0.72 | 0.72 | EUR, 8% | $15,000 | 77 | $4,000 | $1,000 | - |
| PL | Poland | 32.3 | 0.040 | 0.91 | 23.0% | 4.1% | 0.69 | +26 | 11.0% | 0.70 | 14 / 10 | 1.00 | 0.51 | 0.51 | PLN, 8% | $18,000 | 56 | $4,000 | $1,000 | - |
| JP | Japan | 127.5 | 0.142 | 0.91 | 10.0% | 2.4% | 0.45 | -10 | 15.4% | 0.52 | 14 / 10 | 0.70 | 0.78 | 0.78 | JPY, 8% | $20,000 | 42 | $4,000 | $1,000 | - |
| KR | South Korea | 112.2 | 0.132 | 0.87 | 10.0% | 6.5% | 0.44 | -10 | 11.0% | 0.74 | 30 / 30 | 1.00 | 0.73 | 0.73 | KRW, 5% | $20,000 | 60 | $4,000 | $1,000 | - |
| CN | China | 2,044.5 | 0.136 | 0.76 | 9.1% | 0.0% | 0.43 | -7 | 8.0% | 0.68 | 7 / 15 | 1.20 | 0.60 | 0.60 | CNY, 4% | $40,000 | 90 | $6,000 | $1,000 | - |
| IN | India | 138.4 | 0.133 | 0.57 | 18.0% | 22.0% | 0.62 | +4 | 12.0% | 0.54 | 7 / 14 | 1.60 | 0.27 | 0.27 | INR, 3% | $40,000 | 70 | $5,000 | $800 | - |
| ID | Indonesia | 68.2 | 0.113 | 0.62 | 11.0% | 7.9% | 0.53 | -7 | 10.0% | 0.57 | 7 / 10 | 1.00 | 0.56 | 0.56 | IDR, 4% | $40,000 | 120 | $5,000 | $800 | $150,000 |
| CA | Canada | 64.5 | 0.063 | 1.06 | 12.0% | 2.0% | 0.94 | +5 | 15.0% | 1.20 | 14 / 10 | 1.00 | 0.77 | 0.77 | CAD, 5% | $8,000 | 42 | $2,500 | $300 | - |
| MX | Mexico | 44.0 | 0.053 | 0.89 | 16.0% | 25.0% | 0.90 | +11 | 13.0% | 0.65 | 7 / 15 | 1.00 | 0.47 | 0.47 | MXN, 9% | $20,000 | 75 | $4,000 | $1,000 | - |
| BR | Brazil | 77.9 | 0.106 | 0.85 | 25.0% | 25.0% | 1.78 | +18 | 13.0% | 0.50 | 7 / 15 | 1.00 | 0.47 | 0.47 | BRL, 15% | $40,000 | 120 | $5,000 | $1,000 | - |
| AU | Australia | 49.7 | 0.047 | 1.07 | 10.0% | 0.0% | 0.80 | +7 | 13.0% | 1.30 | 14 / 10 | 1.00 | 0.77 | 0.77 | AUD, 7% | $8,000 | 42 | $2,500 | $300 | - |
| TR | Turkey | 48.0 | 0.066 | 0.81 | 20.0% | 25.0% | 0.60 | +8 | 17.0% | 0.65 | 3 / 7 | 1.00 | 0.45 | 0.45 | TRY, 21% | $20,000 | 75 | $4,000 | $1,000 | - |
| SA | Saudi Arabia | 11.4 | 0.015 | 0.81 | 15.0% | 5.7% | 0.57 | +6 | 14.0% | 0.55 | 14 / 10 | 1.00 | 0.80 | 0.80 | SAR, pegged | $12,000 | 60 | $3,500 | $1,000 | - |
| AE | United Arab Emirates | 8.1 | 0.009 | 0.87 | 5.0% | 4.6% | 1.38 | +3 | 15.0% | 0.56 | 14 / 10 | 1.00 | 0.81 | 0.81 | AED, pegged | $12,000 | 45 | $3,500 | $800 | - |

Sources are in the calibration notes; several figures (entry and running costs, some fees and ad prices) are our
estimates where no good source was found.

## The market office

Board tools. The research budget is $250,000, shared by the team. A test is paid when you order it, and runs
when its facility is free. Each category has its own survey panel, ad test account and audit desk, and the team
has 2 test market slots. Only the agent who orders a test can read its result, so share what you learn on the
board. Everyone can see the ledger of tests.

- `company_status`: your company now: the phase (launch, live or ended), when the launch phase ends, how long a
  month takes, how many months have run and when the next starts, the latest day of the books, and a summary of
  each month (the commit that ran, whether the software stopped, revenue, cash). Free.
- `company_books(since_day, until_day)`: the company's books day by day, at most 120 days a call: cash, debt, and
  per product price, ads, stock, rating, reviews, rival prices and yesterday's results, with the news and the
  software's notes, and the same for each country entered (with its `fx`). Free.
- `office_info`, `office_status`: budget, rules, catalog, queue and ledger. Free.
- `plan_review(ref)`: checks `company/plan.yaml` and runs `ops/` for 14 days at `ref` (default `main`). Free.
- `unit_economics(ref)`: costs and fees per unit for the plan's products. Free.
- `quote_test`, `order_test`, `test_result`, `cancel_test`: the tests below. A test that fails is refunded.

The grader reads your company's value at two moments during the run (a third and two thirds of its months) for
the run's record; that runs no software. While the grader runs software (after the run), test markets wait.

| test | cost | real minutes | facility | what it tells you |
|---|---|---|---|---|
| `survey` | $2,000 plus $5 a person | 8 | one per category | Share of a panel of category shoppers who say they would buy the product at each of up to 5 prices you name (each price is shown to an equal part of the panel), the share who say they are interested in this kind of product, and what they usually pay. People overstate how much they would buy. params: prices (list), n (200, 400 or 800 people). |
| `ad_test` | the ad spend plus $1,000 | 10 | one per category | A small campaign for the product as a new brand, on one channel, for 7 or 14 days, at the price you name: daily spend, impressions, clicks, visits, orders and revenue. params: channel (site or marketplace), daily_budget (up to 2000), days (7 or 14), price. |
| `supplier_audit` | $3,000 | 6 | one per category | A sample order of 200 units from one supplier checked for defects, and that supplier's last 8 lead times (days from order to arrival) and fill rates (share of each order shipped). params: supplier (A, B or C, default A). |
| `concept_test` | $15,000 | 8 | one per category | A product designer's prototype of an improved own version of the product, shown to 400 category shoppers next to the current one: the share who prefer the prototype, the share who would pay 15% more for it, and the designer's estimate of the chance a development project works, its months and its supplier price. params: none. |
| `subscription_test` | $6,000 | 6 | one per category | 300 recent buyers of this kind of product asked whether they would subscribe at each of up to 3 discounts you name, and the share of a similar brand's subscribers who left at each of their first 6 renewals. People overstate. params: discounts (list of 0.05 to 0.30). |
| `bundle_test` | $4,000 | 6 | one per category | 400 shoppers of the product shown a bundle with another product at 15% off: the share who would add the other product, and the share who already buy both. params: other (a catalog product id). |
| `buyer_calls` | $5,000 | 6 | one per category | Calls with buyers at 5 retail chains about the product: the price per unit each would pay (after their trade spend), how much of it they expect to sell a month for each unit on the shelf, how much of their stock they usually send back unsold, what their penalties usually cost suppliers, the share of their shoppers who also buy online, and how much brands' own sales usually rise once they are on shelves. params: none. |
| `test_market` | $60,000 | 12 | 2 slots for the team | Runs company/plan.yaml and ops/ at the ref for 182 days in a fresh practice world (one of the shapes mnl, nested, wom) with the true products: weekly results by product and channel, each line's revenue and contribution, cash each week, news, and the software's errors. It may enter only the countries your company has entered. The daily record at GET /api/market/record/<test id> has the company's daily cash and revenue only. target: ignored (use "company"). params: none. |
| `market_report` | $3,000 | 5 | one per category | A category sales report, as sellers buy from market data firms: for each catalog product of the target's category, the estimated units that all sellers together sold in each of the last 3 full months of the live market (a month behind, and within about 20%), your own units, and your share. Before the company opens, an estimate of a month now. target: any product id of the category. params: country (a country code from the manual's section "Countries", entered or not; default US). |

A `test_market` result has weekly totals, each line's revenue and contribution over the 182 days, the operating
profit, and the company value by the grading formula at day 182. The full daily record is at `GET /api/market/record/<id>` on the board,
with your board token.

## Rules

| rule | value |
|---|---|
| seed money | $2,000,000 |
| research budget | $250,000 |
| product lines selling at once | at most 10 |
| launch cost per line | $6,000 |
| overhead | $35,000 every 30 days, or 10% of the average 30 days' revenue over the last 90 days if that is more (staff, service, software and rent grow with the business); charged a little each day from the day the first line launches |
| email and SMS list | $0.015 per own-site customer on the list per 30 days |
| cost per line that is selling | $1,500 per 30 days |
| own site card fees | 2.9% plus $0.30 per order |
| marketplace referral fee | 15% of the price |
| marketplace payout | every 14 days, for sales at least 10 days old |
| supplier deposit | 30% at order, the rest at shipping, 35 days before arrival |
| price break | 8% off at 5 minimum orders or more |
| largest supplier order | 200,000 units |
| credit line | the line in the plan (up to $500,000), drawable each day up to the borrowing base: 50% of the landed cost of stock on hand plus 80% of what the marketplace and chains owe you. Debt above the limit is repaid from cash. 10.5% a year on what you draw, 0.5% a year on the line |
| cash below zero | drawn from the credit line; with the line used up, the company is bankrupt |
| tax | 21% of each year's positive operating profit minus interest, paid at the end of the year |
| development project | $90,000, at most 3 at once |
| retail chains | pay 30 to 90 days after delivery, as the offer says; at most 6 contracts |
| stock at the end | valued at 35% of landed cost |
| profit multiple in company value | 3 times the last 365 days' operating profit |

### Fulfillment and storage

| size | own site fulfillment per order | marketplace fulfillment per unit | storage per unit per 30 days | storage October to December, lines on the marketplace | return shipping per unit | pallet freight to a chain per unit |
|---|---|---|---|---|---|---|
| small | $6.50 | $4.10 | $0.15 | $0.45 | $4.00 | $0.60 |
| medium | $8.50 | $6.20 | $0.45 | $1.35 | $7.00 | $1.20 |
| large | $15.00 | $11.50 | $1.50 | $3.75 | $12.00 | $3.00 |

Some heavy or bulky products pay a higher marketplace fulfillment fee per unit than their size class: f1 (adjustable dumbbells, pair) $28.00; h5 (standing desk converter) $22.00; p1 (orthopedic dog bed, large) $13.50.
Each product's own fees are in `catalog.json` (`fees`) and in `setup`.

### Season

Monthly demand factors by category. A market can make the season stronger or weaker.

| category | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| kitchen | 0.90 | 0.85 | 0.94 | 0.93 | 0.98 | 0.94 | 1.00 | 0.96 | 0.94 | 1.00 | 1.19 | 1.56 |
| pet | 0.97 | 0.92 | 0.99 | 0.98 | 1.00 | 0.98 | 1.02 | 1.00 | 0.97 | 1.00 | 1.08 | 1.15 |
| fitness | 1.40 | 1.15 | 1.02 | 0.95 | 0.95 | 0.90 | 0.92 | 0.90 | 0.92 | 0.90 | 1.00 | 1.04 |
| home_office | 1.12 | 1.00 | 0.98 | 0.94 | 0.92 | 0.90 | 0.98 | 1.12 | 1.10 | 0.98 | 1.00 | 1.04 |
| beauty | 0.92 | 0.98 | 0.94 | 0.94 | 1.00 | 0.92 | 0.96 | 0.94 | 0.92 | 0.98 | 1.18 | 1.52 |
| outdoor | 0.72 | 0.72 | 0.88 | 1.06 | 1.25 | 1.35 | 1.35 | 1.18 | 0.96 | 0.84 | 0.88 | 0.95 |

## The catalog

Typical price is what comparable products sell for today, and what rivals charge at the start.

| id | product | category | supplier price | freight per unit | typical price | min order | quoted lead (days) | size |
|---|---|---|---|---|---|---|---|---|
| k1 | Cast iron skillet, 10 inch | kitchen | $9.50 | $2.10 | $34.00 | 500 | 70 | medium |
| k2 | Pour-over coffee set | kitchen | $7.00 | $1.40 | $29.00 | 300 | 65 | medium |
| k3 | Knife sharpener | kitchen | $4.00 | $0.60 | $22.00 | 500 | 55 | small |
| k4 | Silicone food storage bags, set of 8 | kitchen | $3.20 | $0.50 | $19.00 | 1,000 | 55 | small |
| k5 | Electric milk frother | kitchen | $5.50 | $0.80 | $24.00 | 500 | 65 | small |
| p1 | Orthopedic dog bed, large | pet | $18.00 | $6.50 | $69.00 | 200 | 80 | large |
| p2 | Slow feeder dog bowl | pet | $2.50 | $0.60 | $16.00 | 1,000 | 55 | small |
| p3 | Cat water fountain | pet | $8.00 | $1.60 | $32.00 | 400 | 70 | medium |
| p4 | No-pull dog harness | pet | $4.50 | $0.50 | $25.00 | 500 | 60 | small |
| p5 | Pet hair remover roller | pet | $2.20 | $0.40 | $15.00 | 1,000 | 55 | small |
| f1 | Adjustable dumbbells, pair | fitness | $55.00 | $14.00 | $179 | 100 | 90 | large |
| f2 | Resistance band set | fitness | $3.50 | $0.50 | $22.00 | 1,000 | 55 | small |
| f3 | Yoga mat, 6 mm | fitness | $6.00 | $1.80 | $32.00 | 500 | 65 | medium |
| f4 | Foam roller | fitness | $4.00 | $1.50 | $21.00 | 500 | 65 | medium |
| f5 | Massage gun | fitness | $28.00 | $3.00 | $89.00 | 200 | 75 | medium |
| h1 | Laptop stand | home_office | $7.00 | $1.20 | $35.00 | 500 | 65 | medium |
| h2 | Monitor light bar | home_office | $11.00 | $1.30 | $45.00 | 300 | 70 | medium |
| h3 | Ergonomic footrest | home_office | $9.00 | $2.40 | $38.00 | 300 | 70 | medium |
| h4 | Cable management kit | home_office | $2.80 | $0.40 | $17.00 | 1,000 | 55 | small |
| h5 | Standing desk converter | home_office | $48.00 | $16.00 | $169 | 100 | 90 | large |
| b1 | Facial cleansing brush | beauty | $6.00 | $0.60 | $34.00 | 500 | 60 | small |
| b2 | Hair dryer brush | beauty | $14.00 | $1.80 | $55.00 | 300 | 70 | medium |
| b3 | Jade roller set | beauty | $2.50 | $0.40 | $18.00 | 1,000 | 55 | small |
| b4 | LED makeup mirror | beauty | $9.00 | $2.00 | $39.00 | 300 | 70 | medium |
| b5 | Satin pillowcase set | beauty | $4.00 | $0.50 | $24.00 | 500 | 55 | small |
| o1 | Camping hammock | outdoor | $8.00 | $1.00 | $36.00 | 500 | 65 | medium |
| o2 | Insulated water bottle, 32 oz | outdoor | $5.50 | $1.10 | $29.00 | 500 | 65 | medium |
| o3 | Solar lantern, set of 2 | outdoor | $7.00 | $1.20 | $32.00 | 500 | 70 | medium |
| o4 | Portable camping stove | outdoor | $16.00 | $2.60 | $59.00 | 300 | 75 | medium |
| o5 | Hiking daypack, 25 L | outdoor | $12.00 | $2.20 | $49.00 | 300 | 70 | medium |
