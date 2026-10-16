# GARVEE Catalog Crawler

Dự án Python lấy sản phẩm GARVEE từ sitemap và JSON công khai, lưu SQLite/JSONL và xuất đúng các cột trong `templates/product_template.csv` của bạn.

## Giao diện tiếng Việt

Nút **Tải CSV lần gần nhất** chỉ xuất sản phẩm thuộc danh sách của lần cào gần nhất, bao gồm dữ liệu được tái sử dụng từ kho. Nút **Tải toàn bộ dữ liệu đã lưu** tải `products-all.csv`. Bảng danh mục vẫn hiển thị toàn bộ dữ liệu lịch sử. Nếu lần cào không lấy được sản phẩm, file lần đó chỉ có tiêu đề cột.

Mở `start-ui.cmd` trong thư mục dự án, sau đó mở http://127.0.0.1:8766 trong trình duyệt. Hoặc chạy:

```powershell
.\.venv\Scripts\python -m garvee_crawler.web
```

Chọn sitemap hoặc nhập URL, số lượng sản phẩm và bấm **Bắt đầu thu thập**. **Dừng và lưu kết quả** giữ sản phẩm đã lưu và xuất lại CSV. Bỏ chọn cập nhật để chạy tiếp. Bảng hỗ trợ tìm tên/SKU, phân trang, ảnh, giá và trạng thái tồn hàng ở nguồn. Nút **Tải CSV lần gần nhất** mở sau khi tác vụ kết thúc. Tiến độ tính trên URL cần xử lý của lần chạy, không bao gồm sản phẩm đã có.

Giao diện chỉ phục vụ trên máy hiện tại (127.0.0.1), không cần tài khoản. Đừng chạy CLI cùng lúc với một tác vụ giao diện dùng chung thư mục output. Đóng cửa sổ máy chủ sẽ ngừng phục vụ giao diện.

## Công nghệ

- [Crawlee Python](https://github.com/apify/crawlee-python): điều phối tác vụ và retry.
- [HTTPX](https://github.com/encode/httpx): tải sitemap, robots và JSON qua HTTP.
- [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/): đọc JSON nhúng khi endpoint không hoạt động.
- [Playwright Python](https://github.com/microsoft/playwright-python): tùy chọn thử lại endpoint trong phiên trình duyệt.
- SQLite: lưu thành công ngay sau mỗi sản phẩm, chống trùng URL và tiếp tục sau khi dừng.

Dùng các thư viện qua package chính thức; không sao chép và ghép mã từ nhiều repo. Crawlee và Scrapy cùng làm nhiệm vụ điều phối, vì vậy không cần dùng cả hai.

## Chạy trên Windows PowerShell

Chạy lệnh tại thư mục dự án. Python 3.11 trở lên.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-lock.txt
.\.venv\Scripts\python -m garvee_crawler.cli --limit 30
```

Môi trường `.venv` đã được cài trên máy hiện tại; có thể chạy ngay lệnh cuối.

```powershell
# Toàn bộ URL sản phẩm trong sitemap (có thể mất nhiều giờ)
.\.venv\Scripts\python -m garvee_crawler.cli --limit 0

# Chạy lại cùng lệnh để tiếp tục, bỏ qua sản phẩm thành công
.\.venv\Scripts\python -m garvee_crawler.cli --limit 0

# Cập nhật giá/thông tin đã lưu
.\.venv\Scripts\python -m garvee_crawler.cli --limit 30 --refresh

# Danh sách URL riêng, mỗi dòng một URL
.\.venv\Scripts\python -m garvee_crawler.cli --urls urls.txt --limit 0

# Xuất lại mà không gọi website
.\.venv\Scripts\python -m garvee_crawler.cli --export-only

# Tùy chọn trình duyệt
.\.venv\Scripts\python -m playwright install chromium
.\.venv\Scripts\python -m garvee_crawler.cli --limit 30 --browser-fallback

# Kiểm tra tự động
.\.venv\Scripts\python -m pytest -q
```

## Kết quả trong output/

- `products.csv`: CSV UTF-8 BOM, giữ nguyên thứ tự cột template. Mỗi biến thể một dòng; ảnh dư có dòng riêng cùng handle.
- `products.jsonl`: dữ liệu nguồn đầy đủ, URL và thời điểm UTC; có trạng thái available của từng biến thể.
- `catalog.sqlite`: dữ liệu bền vững để tiếp tục và xuất lại.
- `report.json`: số sản phẩm, dòng CSV, SKU thiếu/trùng và số mục lỗi.
- `errors.json`, `crawl.log`: lỗi và nhật ký.
- `urls.txt`: các URL được chọn trong lần chạy gần nhất.

`--output` đổi thư mục dữ liệu; `--template` đổi CSV mẫu; `--delay` đổi khoảng cách giữa các HTTP request (mặc định 1 giây). products.csv chỉ gồm sản phẩm thuộc lần cào gần nhất; products-all.csv chứa toàn bộ dữ liệu đã lưu. Muốn tập dữ liệu riêng, dùng thư mục output khác. Không chạy hai tiến trình cùng output.

## Quy tắc dữ liệu

- Tự tìm các màu được liên kết bằng ô chọn ảnh trên trang sản phẩm. GARVEE có thể lưu mỗi màu dưới một URL/SKU riêng: bộ cào bổ sung những URL đó vào lần cào hiện tại và giữ thành sản phẩm riêng theo nguồn. Giới hạn số lượng áp dụng cho URL đầu vào; tổng kết quả có thể lớn hơn để bao gồm đủ màu. Chọn cập nhật để quét lại liên kết màu đã lưu. Chưa tự gộp các URL riêng thành một sản phẩm Shopify nhiều biến thể.

- Giá endpoint `.js` là cent: 11999 → 119.99. Không tự áp coupon. CSV không có cột tiền tệ; cần đối chiếu tiền tệ cửa hàng đích trước khi nhập.
- Sản phẩm xuất mặc định Draft, Published FALSE để kiểm tra trước khi đăng.
- Không suy diễn giá vốn, thuế cửa hàng đích, phân loại Google/Shopify hay kích thước đóng gói. Các cột này để trống.
- `Type` và tags giữ dữ liệu nguồn, chưa chuyển sang taxonomy Shopify. Tags nguồn có thể chứa nhãn vận hành của GARVEE.
- Trọng lượng biến thể dùng giá trị gram của nguồn; không coi là trọng lượng kiện hàng.
- JSON gốc giữ barcode dạng chuỗi, SKU, available, ảnh, mô tả HTML. Khi available=false, CSV áp chính sách không bán: Inventory quantity=0, Inventory tracker=shopify, Continue selling when out of stock=deny, Fulfillment service=manual. Đây là quy tắc nhập ở cửa hàng đích để phản ánh không thể mua tại nguồn, không phải số lượng kho thực tế GARVEE. Khi available=true/không rõ, số lượng vẫn trống; cần cấu hình kho đích trước khi đăng bán. Cửa hàng nhiều địa điểm cần xử lý tồn kho theo từng địa điểm riêng.
- Compare-at price chỉ xuất khi lớn hơn Price; giá so sánh bằng 0, bằng hoặc thấp hơn giá bán được để trống. Dữ liệu gốc không thay đổi.
- Chỉ thu thập URL sản phẩm GARVEE, kiểm tra robots.txt, giới hạn tốc độ và retry lỗi mạng/429/5xx. Redirect và lỗi được ghi lại, không coi là thành công.
- Endpoint/HTML không có JSON biến thể đầy đủ thì ghi lỗi, không tạo giá hoặc SKU giả. Browser fallback không bảo đảm giải quyết mọi lỗi website.
- URL cũ chuyển hướng trong GARVEE được giải quyết tối đa 5 bước, lưu alias và dữ liệu theo URL đích để tránh trùng. Chuyển hướng ra ngoài GARVEE bị từ chối.
- Không xóa sản phẩm cũ khỏi database nếu chúng biến mất khỏi sitemap. Muốn snapshot sạch, dùng output mới.

## Phạm vi kiểm chứng

Xem `output/report.json` cho kết quả chạy thực tế. Các test kiểm tra giá, nhiều biến thể/ảnh, CSV chứa dấu phẩy/xuống dòng, URL và dữ liệu thiếu. Việc nhập CSV vào cửa hàng Shopify đích và browser fallback cần kiểm chứng riêng trên môi trường sử dụng.

