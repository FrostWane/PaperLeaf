import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LoginForm } from "@/components/login-form";

describe("LoginForm", () => {
  it("阻止提交不合法邮箱和短密码", async () => {
    render(<LoginForm />);
    fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "bad" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "123" } });
    fireEvent.click(screen.getByRole("button", { name: /进入工作区/ }));
    await waitFor(() => expect(screen.getByText("请输入有效邮箱")).toBeInTheDocument());
    expect(screen.getByText("密码至少 8 位")).toBeInTheDocument();
  });
});
