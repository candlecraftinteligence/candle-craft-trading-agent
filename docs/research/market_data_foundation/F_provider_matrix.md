# F. Provider matrix

Access date for this document: 2026-10-19. Seed URLs are the assignment's primary docs. A live call is recorded only in the attached online summary. Documentation availability is not an entitlement.

| interface | role in CCI | exercised by this phase | rights / cost | blocker |
| --- | --- | --- | --- | --- |
| Binance USD-M public REST | production candles, ticker, funding history, OI, account ratio | code audit; live only if the sample summary says observed | public market data. Commercial redistribution of stored payloads is not established here. | none for public REST; shared IP spare capacity unknown |
| Bybit V5 public REST | production linear client | code audit; live only if the sample summary says observed | public market data. Commercial redistribution not established. | OI unit and single-versus-both convention not proven from the repository DTO |
| CoinPaprika v1 | production ranks and BTC dominance | code audit; live only if the sample summary says observed | public API. FAQ should be re-read before publishing stored tickers. | full ticker payload may exceed the response cap |
| CMC keyless REST | shadow listings and global metrics only | shadow adapter; live only if the sample summary says observed | keyless GET, no key header. Numeric free quota is not invented. Commercial use and redistribution need a later reading of the current license. | not a production source |
| CMC authenticated REST and derivatives | not integrated | not called | `X-CMC_PRO_API_KEY` is a different credential from MCP. Derivatives market pairs are not assumed keyless. | no entitlement used |
| CMC MCP | research interface only | see G | separate header and entitlement | configuration is not a connection |
| CoinGlass | not integrated | documentation and synthetic contracts only | plan resolution, retention, and commercial rights are unresolved | no authorized no-overage entitlement, so no call |

Revalidated on 2026-10-19 from the keyless public API page: the root is `https://pro-api.coinmarketcap.com/public-api`, GET only, no API-key header, and the rate pool is IP-based. The page does not state a numeric free quota. It documents `/v3/cryptocurrency/listings/latest` and `/v1/global-metrics/quotes/latest`, plus the canonical map and metadata paths. CMC100 is a separate index endpoint, not the Top-100 listings. The keyless standard list on that page does not include the `/v5` derivatives market-pair routes, so those remain untested and not assumed keyless. Authenticated REST still uses `X-CMC_PRO_API_KEY`, which was not sent.

Bybit's open-interest page, retrieved the same day, defines `openInterest` as the sum of both sides and `singleOpenInterest` as one side. For linear BTCUSDT the documented unit is BTC. Inverse BTCUSD is documented in USD. The repository stores `openInterest` and leaves `unit` as N/A.

Same-venue exchange data remains the candidate authority for that venue's candles, trades, mark, index, funding, OI, positioning, taker flow, and book. That is a recommendation to test, not a result of this sample.

Global rank and dominance are methodology measurements. CoinPaprika stays in production during this phase.

Questions for a later licensing pass, not sent to the providers: whether stored CMC or CoinPaprika responses may be committed; whether derived commercial signals require a paid contract; whether CoinGlass historical liquidations can be retained.
