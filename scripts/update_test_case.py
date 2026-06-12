
import requests
import json

# 配置
BASE_URL = "http://127.0.0.1:8000"  # 假设应用程序在本地运行
CASE_ID = 1

def get_test_case(case_id):
    """根据ID获取测试用例。"""
    url = f"{BASE_URL}/api/testcases/{case_id}"
    response = requests.get(url)
    response.raise_for_status()  # 对错误状态码抛出异常
    return response.json()

def update_test_case(case_id, case_data):
    """用新数据更新测试用例。"""
    url = f"{BASE_URL}/api/testcases/{case_id}"
    
    # 我们需要以TestCaseUpdate期望的格式发送数据
    # 包括名称、描述、module_id和步骤。
    update_payload = {
        "name": case_data["name"],
        "description": case_data["description"],
        "module_id": case_data["module_id"],
        "steps": case_data["steps"]
    }
    
    response = requests.put(url, json=update_payload)
    response.raise_for_status()
    return response.json()

def main():
    """主函数，用于获取、修改和更新测试用例。"""
    try:
        # 1. 获取当前测试用例数据
        print(f"获取测试用例 {CASE_ID}...")
        test_case = get_test_case(CASE_ID)
        print("测试用例数据获取成功。")
        
        # 2. 定义修正后的完整步骤列表
        print("定义修正后的步骤...")
        corrected_steps = [
            {
                "step_order": 1,
                "keyword": "goto",
                "locator": "",
                "value": "https://www.baidu.com",
                "description": "打开百度首页"
            },
            {
                "step_order": 2,
                "keyword": "fill",
                "locator": "#kw",
                "value": "Gemini",
                "description": "在搜索框输入Gemini"
            },
            {
                "step_order": 3,
                "keyword": "click",
                "locator": "#su",
                "value": "",
                "description": "点击搜索按钮"
            },
            {
                "step_order": 4,
                "keyword": "expect_text",
                "locator": "h3 > a", # 更稳健的选择器
                "value": "Gemini",
                "description": "验证第一个结果的标题"
            }
        ]

        # 将新步骤分配给测试用例数据
        test_case["steps"] = corrected_steps
        print("步骤已替换。")

        # 3. 用修正后的数据更新测试用例
        print("更新测试用例...")
        updated_case = update_test_case(CASE_ID, test_case)
        print("测试用例更新成功！")
        # print("更新后的数据:", json.dumps(updated_case, indent=2))

    except requests.exceptions.RequestException as e:
        print(f"发生API错误：{e}")
    except Exception as e:
        print(f"发生意外错误：{e}")

if __name__ == "__main__":
    main()
