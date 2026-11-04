# claude-macro-radar-data

Số liệu theo ngày cho trang Solana của app **macro-radar-claude**. Job `sol-daily` (GitHub Actions) chạy 3 giờ một lần, lấy số từ các API công khai và ghi vào `data/sol_daily.json`. Bản review đêm (23:30 giờ Việt Nam) chép file này vào DB của app.

| Id | Chỉ số | Đơn vị | Nguồn |
|---|---|---|---|
| `SOL.w_dex` | Khối lượng giao dịch spot trên DEX | USD tỷ/ngày | DefiLlama |
| `SOL.w_rev` | REV = phí giao dịch của chain + tip MEV | USD triệu/ngày | DefiLlama |
| `SOL.w_app_rev` | Doanh thu ứng dụng | USD triệu/ngày | DefiLlama |
| `SOL.w_app_fees` | Phí người dùng trả cho ứng dụng | USD triệu/ngày | DefiLlama |
| `SOL.w_etf` | Dòng tiền ròng ETF SOL giao ngay tại Mỹ (ngày giao dịch) | USD triệu/ngày | Farside Investors |
| `SOL.d_tvl` | TVL DeFi | USD tỷ | DefiLlama |
| `SOL.d_stables` | Vốn hóa stablecoin trên Solana | USD tỷ | DefiLlama |

Quy tắc:

- Mỗi điểm là một ngày UTC đã kết thúc. Số của hôm nay (chưa trọn ngày) nằm ở `data/sol_latest.json`.
- Điểm cũ được giữ lại. Vài ngày gần nhất được ghi đè bằng số mới nhất, vì nguồn còn sửa số trong vài ngày.
- Nguồn nào lỗi thì chuỗi đó giữ nguyên và lỗi được ghi ở `errors`. `data/probe.json` ghi lại phản hồi của từng nguồn để kiểm tra định nghĩa.

Chạy tay: tab **Actions → sol-daily → Run workflow**, hoặc `python3 scripts/fetch_sol_daily.py` trên máy có Internet (Python 3.10+, không cần cài thêm gói).

Đây là số liệu tham khảo, không phải lời khuyên đầu tư.
