"""Visible desktop controls stay available after the localhost server stops."""
import argparse
import threading
import time
import tkinter as tk
import webbrowser

from .lifecycle import WEB, healthy, start_app, stop_app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--start",action="store_true")
    args=parser.parse_args()
    window=tk.Tk()
    window.title("АТЛАС · управление")
    window.geometry("480x320")
    window.resizable(False,False)
    window.configure(bg="#1d223a")
    status=tk.StringVar(value="Web уже работает" if healthy() else "Приложение остановлено")
    tk.Label(window,text="АТЛАС. Знания",font=("Segoe UI",22,"bold"),fg="white",bg="#1d223a").pack(pady=(22,5))
    tk.Label(window,text="Веб-интерфейс и локальная Qwen",font=("Segoe UI",11),fg="#bfc6e5",bg="#1d223a").pack()
    tk.Label(window,textvariable=status,font=("Segoe UI",10),fg="#d9ddf4",bg="#1d223a",wraplength=430,height=3).pack(pady=12)
    frame=tk.Frame(window,bg="#1d223a");frame.pack()
    buttons=[]
    state={"busy":False,"closed":False}
    def message(value):
        if state["closed"]:return
        try:window.after(0,status.set,value)
        except (RuntimeError,tk.TclError):pass
    def work(action,close=False):
        if state["busy"]:
            if close:message("Дождитесь завершения текущего запуска/остановки, затем закройте окно.")
            return
        state["busy"]=True
        for button in buttons:button.configure(state="disabled")
        def run():
            success=False
            try:
                message(action(progress=message));success=True
            except Exception as exc:message("Ошибка: "+str(exc)[:180])
            finally:
                def done():
                    state["busy"]=False
                    if close and success:
                        state["closed"]=True;window.destroy()
                    else:
                        for button in buttons:button.configure(state="normal")
                window.after(0,done)
        threading.Thread(target=run,daemon=True).start()
    def observe():
        while not state["closed"]:
            running=healthy()
            if not state["busy"]:
                message("Web работает. Модель сохраняется между вопросами." if running else "Приложение остановлено. Можно запустить снова.")
            time.sleep(3)
    for text,color,command in [("▶ Запустить всё","#8066f2",lambda:work(start_app)),("■ Остановить всё","#a64962",lambda:work(stop_app))]:
        button=tk.Button(frame,text=text,command=command,bg=color,fg="white",activebackground=color,font=("Segoe UI",11,"bold"),relief="flat",padx=15,pady=11)
        button.pack(side="left",padx=6);buttons.append(button)
    tk.Button(window,text="Открыть сайт · localhost:8770",command=lambda:webbrowser.open(WEB),font=("Segoe UI",10),bg="#1d223a",fg="#cfc7ff",relief="flat").pack(pady=12)
    tk.Label(window,text="Закрытие этого окна останавливает Web и выгружает Qwen.",font=("Segoe UI",9),fg="#aeb6d6",bg="#1d223a").pack()
    window.protocol("WM_DELETE_WINDOW",lambda:work(stop_app,close=True))
    if args.start:window.after(100,lambda:work(start_app))
    threading.Thread(target=observe,daemon=True).start()
    window.mainloop()


if __name__=="__main__":main()
